#!/usr/bin/env python3
"""ChestTracker: overlay que conta os baús do Fortnite pelo tempo segurando a tecla de interação.

Abre uma janela para escolher atalhos e ajustes (salvos em config.json) e depois o overlay.
Requisitos: pip install pynput pillow   |   Fortnite em "Tela cheia em janela" (sem borda).
"""
import argparse
import ctypes
import functools
import json
import logging
import math
import os
import re
import sys
import threading
import time
from pathlib import Path

try:
    from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont
except ImportError:  # avisado no main()
    Image = None

log = logging.getLogger("baus")
BAUS_JA_ABERTOS = 1000  # baús abertos antes do contador existir
# No .exe (PyInstaller) o __file__ fica numa pasta temporária apagada ao fechar; salva ao lado do .exe.
PASTA = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
RECURSOS = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))  # arquivos embutidos no .exe
LOG_FORTNITE = Path(os.environ.get("LOCALAPPDATA", "")) / "FortniteGame" / "Saved" / "Logs" / "FortniteGame.log"


class Contador:
    """Conta 1 baú quando a tecla fica segurada entre `minimo` e `maximo` segundos."""

    def __init__(self, minimo, maximo, inicial=0):
        self.minimo, self.maximo = minimo, maximo
        self.total = inicial
        self.sessao = 0      # baús desde que o contador abriu
        self.partida = 0     # baús desde que a partida atual começou
        self.inicio = None   # hora do 1º press; auto-repeat do Windows é ignorado
        self.ultima = None   # duração do último hold (serve pra calibrar)
        self.lock = threading.Lock()

    def press(self, t):
        with self.lock:
            if self.inicio is None:
                self.inicio = t

    def release(self, t):
        with self.lock:
            if self.inicio is None:
                return False
            dur, self.inicio = t - self.inicio, None
            self.ultima = dur
            if self.minimo <= dur <= self.maximo:
                self.total += 1
                self.sessao += 1
                self.partida += 1
                return True
            return False

    def ajustar(self, delta):
        with self.lock:
            self.total = max(0, self.total + delta)
            self.sessao = max(0, self.sessao + delta)
            self.partida = max(0, self.partida + delta)

    def nova_partida(self):
        with self.lock:
            self.partida = 0

    def zerar(self):
        with self.lock:
            self.total = 0

    def segurando(self, t):
        with self.lock:
            return None if self.inicio is None else t - self.inicio


def chance_token(baus, um_em):
    """Chance de já ter caído pelo menos 1 token pirata em `baus` baús, com chance de 1 em `um_em` por baú."""
    return 1 - (1 - 1 / um_em) ** baus


def texto_chance(p):
    """0.3934 -> "39,3%", 0.0072 -> "0,72%". Nunca mostra 100%: a chance nunca é garantida."""
    pct = min(p * 100, 99.9)
    return f"{pct:.{2 if pct < 10 else 1}f}%".replace(".", ",")


PRONTO = re.compile(r"\] (.+) ready status changed from .+ to (.+) \(")
NOME = re.compile(r"Successfully logged in user\..*DisplayName=\[(.+?)\]")


# Linhas de quando o jogo fecha. Não serve qualquer "LogExit:": o Fortnite também escreve
# "LogExit: Name:IpNetDriver... shut down" no meio da partida, quando fecha uma conexão.
FIM_DO_JOGO = ("]LogExit: Exiting.", "]LogExit: Game engine shut down", "]Log file closed")


class Fase:
    """Sabe pelo log do Fortnite se o jogador está no lobby, na fila ou em partida."""

    def __init__(self):
        self.partidas = 0  # partidas começadas desde que o contador abriu
        self.limpar()

    def limpar(self):
        self.base = None   # "lobby", "partida", "fechado" ou None (sem log)
        self.fila = False
        self.nome = None   # nome do jogador: o "pronto" dos amigos do grupo não conta

    @property
    def atual(self):
        return "fila" if self.fila else self.base

    def pode_contar(self):
        """Segurar E só soma em partida; sem log (fase desconhecida) continua contando."""
        return self.atual in ("partida", None)

    def linha(self, s):
        """Atualiza a fase com uma linha do log; devolve True quando entrou numa partida nova."""
        if "Load map complete " in s:
            self.fila = False
            if s.rstrip().endswith("/Frontend"):  # /Game/Maps/Frontend é o lobby
                self.base = "lobby"
                return False
            self.base = "partida"
            self.partidas += 1
            return True
        if " ready status changed from " in s:
            m = PRONTO.search(s)
            if m and self.nome in (None, m[1]):
                self.fila = m[2] == "Ready"  # Not Ready, Sitting Out e Away saem da fila
        elif any(m in s for m in FIM_DO_JOGO):
            self.base, self.fila = "fechado", False
        elif "Successfully logged in user." in s:
            m = NOME.search(s)
            self.nome = m[1] if m else None
        return False


class LeitorLog:
    """Lê as linhas novas do log, como `tail -f`.

    Abre e fecha o arquivo a cada leitura: um arquivo aberto impediria o Fortnite de renomear
    o log antigo para backup quando o jogo abre.
    """

    def __init__(self, caminho, bloco=4 << 20):
        self.caminho, self.bloco = caminho, bloco
        self.pos, self.resto, self.reiniciou = 0, b"", False

    def novas(self):
        """Linhas completas novas ([] se nada mudou) ou None se o log não existe."""
        self.reiniciou = False
        try:
            tam = self.caminho.stat().st_size
            if tam < self.pos:  # shortcut: o log novo só é notado se for menor que o antigo; a cada 0,5 s sempre é
                self.pos, self.resto, self.reiniciou = 0, b"", True
            if tam == self.pos:
                return []
            with open(self.caminho, "rb") as f:
                f.seek(self.pos)
                dados = f.read(min(tam - self.pos, self.bloco))
        except OSError:
            self.pos, self.resto = 0, b""
            return None
        self.pos += len(dados)
        partes = (self.resto + dados).split(b"\n")
        self.resto = partes.pop()  # linha ainda sendo escrita
        return [p.decode("utf-8", "replace") for p in partes]


def iniciar_fase(fase, cont, caminho=LOG_FORTNITE):
    """Acompanha o log do Fortnite numa thread e zera os baús da partida quando outra começa."""
    leitor = LeitorLog(caminho)

    def rodar():
        while linhas := leitor.novas():  # alcança o que já estava escrito, sem contar partidas antigas
            for s in linhas:
                fase.linha(s)
        fase.partidas = int(fase.base == "partida")
        log.info("Log do Fortnite: %s | fase: %s", caminho, fase.atual)
        while True:
            time.sleep(0.5)
            linhas = leitor.novas()
            if linhas is None or leitor.reiniciou:
                fase.limpar()
            antes = fase.atual
            for s in linhas or ():
                if fase.linha(s):
                    cont.nova_partida()
            if fase.atual != antes:
                log.info("Fase: %s", fase.atual)

    threading.Thread(target=rodar, daemon=True).start()


def por_hora(baus, segundos, aquecimento=120):
    """Ritmo em baús/hora; None nos primeiros 2 min, quando o número ainda pula demais."""
    return None if segundos < aquecimento else round(baus * 3600 / segundos)


def carregar(arquivo):
    try:
        return int(json.loads(arquivo.read_text(encoding="utf-8"))["baus"])
    except FileNotFoundError:
        return BAUS_JA_ABERTOS
    except (ValueError, KeyError, TypeError) as e:
        log.warning("Arquivo %s inválido (%s), começando de %d", arquivo, e, BAUS_JA_ABERTOS)
        return BAUS_JA_ABERTOS


def salvar(arquivo, total):
    salvar_json(arquivo, {"baus": total})


def salvar_json(arquivo, dados):
    tmp = arquivo.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(dados, indent=2), encoding="utf-8")
        tmp.replace(arquivo)  # escrita atômica: não corrompe se fechar no meio
    except OSError as e:
        log.error("Falha ao salvar %s: %s", arquivo, e)


def clique_atravessa(root):
    """Deixa o mouse passar pela janela, nunca pega o foco do jogo e mantém o botão na barra (Windows).

    Devolve o hwnd, usado por manter_no_topo.
    """
    GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT = -20, 0x80000, 0x20
    WS_EX_TOOLWINDOW, WS_EX_APPWINDOW, WS_EX_NOACTIVATE = 0x80, 0x40000, 0x8000000
    u32 = ctypes.windll.user32
    hwnd = u32.GetParent(root.winfo_id())
    estilo = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    estilo = (estilo | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_APPWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_TOOLWINDOW
    u32.SetWindowLongW(hwnd, GWL_EXSTYLE, estilo)
    # janela sem borda (overrideredirect) só ganha botão na barra depois de esconder e mostrar de novo
    root.withdraw()
    root.after(10, root.deiconify)
    return hwnd


def manter_no_topo(hwnd):
    """Põe o painel por cima do jogo sem ativá-lo.

    O `attributes("-topmost")` do Tk chama SetWindowPos sem SWP_NOACTIVATE: repetido a cada quadro,
    ele pode tirar o foco do jogo, e o teclado e o mouse param de ir para o Fortnite.
    """
    HWND_TOPMOST, SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = -1, 0x1, 0x2, 0x10
    ctypes.windll.user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE)


# Atalhos: cada ação tem 2 espaços; "" = vazio. Nomes: letra ("e"), tecla do pynput ("f8") ou "mouse3/4/5".
PADRAO = {"atalhos": {"segurar": ["e", ""], "somar": ["f8", "mouse4"], "descontar": ["f7", ""],
                      "zerar": ["f9", ""], "sair": ["f10", ""]},
          "min": 0.85, "max": 3.0, "escala": 1.0, "x": 30, "y": 30,
          "token": 137500}  # chance do token pirata: 1 em N baús
ACOES = (("segurar", "Abrir baú (segurar)"), ("somar", "Somar +1"), ("descontar", "Descontar -1"),
         ("zerar", "Zerar total"), ("sair", "Fechar contador"))
ROTULOS = dict(ACOES)


def copia(dados):
    return json.loads(json.dumps(dados))


def validar(cfg):
    """Mensagem do primeiro problema da config, ou None se estiver tudo certo."""
    if not 0 < cfg["min"] < cfg["max"]:
        return "O tempo mínimo precisa ser maior que 0 e menor que o máximo."
    if not 0.3 <= cfg["escala"] <= 5:
        return "A escala precisa ficar entre 0.3 e 5."
    if not cfg["token"] >= 1:
        return "A chance do token precisa ser 1 em 1 ou mais."
    for acao in ("segurar", "sair"):
        if not any(cfg["atalhos"][acao]):
            return f"Escolha um atalho para {ROTULOS[acao]}."
    return None


def carregar_config(arquivo):
    """Lê config.json por cima do PADRAO; o que vier inválido fica no padrão."""
    cfg = copia(PADRAO)
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return cfg
    except (OSError, ValueError) as e:
        log.warning("Config %s inválida (%s), usando o padrão", arquivo, e)
        return cfg
    if not isinstance(dados, dict):
        return cfg
    for chave in ("min", "max", "escala", "x", "y", "token"):
        v = dados.get(chave)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            cfg[chave] = v
    atalhos = dados.get("atalhos")
    for acao, nomes in (atalhos.items() if isinstance(atalhos, dict) else ()):
        if acao in cfg["atalhos"] and isinstance(nomes, list) and len(nomes) == 2 \
                and all(isinstance(n, str) for n in nomes):
            cfg["atalhos"][acao] = nomes
    return cfg if validar(cfg) is None else copia(PADRAO)


def exibir(nome, curto=False):
    """Nome do atalho como aparece na tela: "e" -> "E", "mouse4" -> "MOUSE 4" (ou "M4")."""
    if not nome:
        return "—"
    if nome.startswith("mouse"):
        return ("M" if curto else "MOUSE ") + nome[5:]
    return nome.replace("_", " ").upper()


def texto_atalhos(atalhos):
    """Rodapé do painel: "F8/M4 +1   F7 -1   F9 ZERA   F10 SAI"."""
    partes = []
    for acao, sufixo in (("somar", "+1"), ("descontar", "-1"), ("zerar", "ZERA"), ("sair", "SAI")):
        nomes = [exibir(n, curto=True) for n in atalhos[acao] if n]
        if nomes:
            partes.append(f"{'/'.join(nomes)} {sufixo}")
    return "   ".join(partes)


def atribuir(atalhos, acao, i, nome):
    """Coloca `nome` no espaço i da ação; se ele estava em outra ação, sai de lá. Devolve o aviso."""
    aviso = f"{exibir(nome)} agora é {ROTULOS[acao]}."
    for outra, nomes in atalhos.items():
        for j, n in enumerate(nomes):
            if n == nome and (outra, j) != (acao, i):
                nomes[j] = ""
                aviso += f" Saiu de {ROTULOS[outra]}."
    atalhos[acao][i] = nome
    return aviso


def nome_tecla(key):
    from pynput import keyboard
    if isinstance(key, keyboard.Key):
        return key.name  # "f8", "space", "shift", ...
    vk, c = getattr(key, "vk", None), getattr(key, "char", None)
    if vk is not None and (0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A):
        return chr(vk).lower()  # letra/número pelo código: igual com Shift, Ctrl ou Caps Lock
    if c:
        return c.lower()
    return f"vk{vk}" if vk is not None else None


def nome_botao(botao):
    """Só meio, 4 e 5: esquerdo e direito disparariam a cada clique no jogo."""
    from pynput import mouse
    nomes = {getattr(mouse.Button, a, None): n for a, n in (("middle", "mouse3"), ("x1", "mouse4"), ("x2", "mouse5"))}
    nomes.pop(None, None)  # x1/x2 só existem no Windows
    return nomes.get(botao)


class Entrada:
    """Transforma tecla/botão apertado em ação do contador, conforme os atalhos."""

    def __init__(self, cont, atalhos, ao_mudar, ao_sair, pode_contar=lambda: True):
        self.cont, self.ao_mudar, self.ao_sair, self.pode_contar = cont, ao_mudar, ao_sair, pode_contar
        self.acoes = {n: acao for acao, nomes in atalhos.items() for n in nomes if n}
        self.apertadas = set()

    def descer(self, nome, t):
        if nome is None or nome in self.apertadas:
            return  # auto-repeat de tecla segurada
        self.apertadas.add(nome)
        acao = self.acoes.get(nome)
        if acao == "segurar":
            if self.pode_contar():  # no lobby e na fila o E não conta
                self.cont.press(t)
            else:
                log.info("Segurou %s fora da partida: não conta", exibir(nome))
        elif acao == "somar":
            self.cont.ajustar(+1)
            self.ao_mudar()
        elif acao == "descontar":
            self.cont.ajustar(-1)
            self.ao_mudar()
        elif acao == "zerar":
            self.cont.zerar()
            self.ao_mudar()
        elif acao == "sair":
            self.ao_sair()

    def subir(self, nome, t):
        self.apertadas.discard(nome)
        if self.acoes.get(nome) != "segurar" or self.cont.segurando(t) is None:
            return  # soltou sem ter apertado com o contador aberto
        contou = self.cont.release(t)
        log.info("Segurou %s por %.2fs -> %s (total %d)", exibir(nome), self.cont.ultima,
                 "BAÚ +1" if contou else "ignorado", self.cont.total)
        if contou:
            self.ao_mudar()


# Códigos de tecla do Windows (VK_*) para os nomes salvos no config.json
VK_NOMES = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backspace": 0x08, "caps_lock": 0x14,
            "shift": 0xA0, "shift_l": 0xA0, "shift_r": 0xA1, "ctrl": 0xA2, "ctrl_l": 0xA2, "ctrl_r": 0xA3,
            "alt": 0xA4, "alt_l": 0xA4, "alt_r": 0xA5, "alt_gr": 0xA5, "cmd": 0x5B, "cmd_l": 0x5B, "cmd_r": 0x5C,
            "menu": 0x5D, "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23, "page_up": 0x21,
            "page_down": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "num_lock": 0x90,
            "scroll_lock": 0x91, "pause": 0x13, "print_screen": 0x2C,
            "mouse3": 0x04, "mouse4": 0x05, "mouse5": 0x06, **{f"f{i}": 0x6F + i for i in range(1, 25)}}


def vk_de(nome, vk_char=None):
    """Código VK do atalho, ou None se não der para ler. vk_char traduz símbolos (",", ";") no Windows."""
    if nome in VK_NOMES:
        return VK_NOMES[nome]
    if nome.startswith("vk") and nome[2:].isdigit():
        return int(nome[2:])
    if len(nome) == 1 and nome.isascii() and nome.isalnum():
        return ord(nome.upper())
    if len(nome) == 1 and vk_char:
        vk = vk_char(ord(nome))
        return None if vk == -1 else vk & 0xFF
    return None


def ler_teclas(entrada, nomes, apertada, intervalo=0.01, rodando=lambda: True, relogio=time.monotonic):
    """Lê o estado dos atalhos a cada 10 ms e avisa a Entrada quando um desce ou sobe.

    Não usa gancho global de teclado/mouse (pynput): com gancho, todo movimento do mouse e toda
    tecla do PC passam por este programa antes de chegar ao jogo, e qualquer demora dele trava
    a entrada do Windows inteiro (o mouse "agarra", teclas ficam presas).
    """
    vks, vk_char = {}, ctypes.windll.user32.VkKeyScanW if sys.platform == "win32" else None
    for nome in nomes:
        vk = vk_de(nome, vk_char)
        if vk is None:
            log.warning("Atalho %s não pode ser lido; escolha outra tecla", exibir(nome))
        else:
            vks[nome] = vk
    baixas = set()
    while rodando():
        t = relogio()
        for nome, vk in vks.items():
            baixo = apertada(vk)
            try:
                if baixo and nome not in baixas:
                    baixas.add(nome)
                    entrada.descer(nome, t)
                elif not baixo and nome in baixas:
                    baixas.discard(nome)
                    entrada.subir(nome, t)
            except Exception:  # um erro não pode matar a leitura das teclas
                log.exception("Erro ao tratar %s", nome)
        time.sleep(intervalo)


def iniciar_entrada(cont, cfg, arquivo, estado, fase):
    def mudou():
        log.info("Total %d", cont.total)
        salvar(arquivo, cont.total)

    e = Entrada(cont, cfg["atalhos"], mudou, lambda: estado.update(sair=True), fase.pode_contar)
    if sys.platform == "win32":
        nomes = {n for ns in cfg["atalhos"].values() for n in ns if n}
        get = ctypes.windll.user32.GetAsyncKeyState
        threading.Thread(target=ler_teclas, args=(e, nomes, lambda vk: get(vk) & 0x8000), daemon=True).start()
        return

    from pynput import keyboard, mouse  # fora do Windows: ganchos do pynput

    def clique(x, y, botao, apertou):  # sem return: False pararia o listener
        (e.descer if apertou else e.subir)(nome_botao(botao), time.monotonic())

    keyboard.Listener(on_press=lambda k: e.descer(nome_tecla(k), time.monotonic()),
                      on_release=lambda k: e.subir(nome_tecla(k), time.monotonic())).start()
    mouse.Listener(on_click=clique).start()  # listeners do pynput já são daemon


# Paleta do HUD do Fortnite: azul profundo, amarelo lendário, contorno preto grosso.
CHAVE = "#010101"  # cor que vira transparente
AMARELO, BRANCO, PRETO = "#FFD84D", "#FFFFFF", "#000000"
AZUL_TOPO, AZUL_NOITE, AZUL_FAIXA, CIANO = "#2456D6", "#0A1640", "#050B22", "#3FD0FF"
VERDE, VERMELHO, CINZA, ANEL = "#7CFC00", "#FF4D4D", "#AEB8D6", "#16224F"
OURO = ("#FFF4B8", "#FFD84D", "#FFA800")  # degradê dos números grandes
TECLA, TECLA_HOVER, TECLA_VAZIA, CARTAO, BORDA = "#2456D6", "#3A6BEA", "#101B48", "#0E1A4A", "#2A3A78"

# Baú em pixel art 16x16: o mesmo desenho do ícone do programa.
BAU_CORES = {"k": "#1A0F05", "h": "#FFE58A", "l": "#FFC93C", "d": "#8B5A12", "b": "#E8A317",
             "s": "#B97C0E", "w": "#FFF3B0", "o": "#5A3A0A"}
_MEIO = ("hhdhhhhhhdhh", "lldlllllldll", "lldlkkkkldll", "kkkkwwwwkkkk", "bbdkwoowkdbb",
         "bbdkwwwwkdbb", "bbdbkkkkbdbb", "bbdbbbbbbdbb", "bbdbbbbbbdbb", "ssdssssssdss")
BAU = (("." * 16,) * 2 + ("..kkkkkkkkkkkk..",) + tuple(".k" + m + "k." for m in _MEIO)
       + ("..kkkkkkkkkkkk..",) + ("." * 16,) * 2)


def misturar(a, b, f):
    """Cor entre a e b (f de 0 a 1)."""
    ca, cb = (int(a[i:i + 2], 16) for i in (1, 3, 5)), (int(b[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(x + (y - x) * f):02X}" for x, y in zip(ca, cb))


def alfa(cor, a):
    """"#RRGGBB" com opacidade a (0 a 1), para desenhar translúcido."""
    return f"{cor}{round(max(0.0, min(a, 1.0)) * 255):02X}"


# Desenho suavizado: o canvas do Tk no Windows não suaviza bordas, então tudo é desenhado com o
# Pillow em SS vezes o tamanho e reduzido no fim (bordas, anel e contorno das letras ficam lisos).
SS = 2
FONTE_TITULO = RECURSOS / "assets" / "Anton-Regular.ttf"
FONTES_TEXTO = (Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "seguisb.ttf",
                Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
_fontes = {}


def fonte(px, texto=False):
    """Anton (embutida) para o HUD; Segoe UI Semibold para frases. Cai na padrão do Pillow se faltar."""
    chave = (max(1, round(px)), texto)
    if chave not in _fontes:
        for arq in (FONTES_TEXTO if texto else ()) + (FONTE_TITULO,):
            try:
                _fontes[chave] = ImageFont.truetype(str(arq), chave[0])
                break
            except OSError:
                continue
        else:
            log.warning("Fonte %s não encontrada, usando a padrão", FONTE_TITULO)
            _fontes[chave] = ImageFont.load_default(chave[0])
    return _fontes[chave]


@functools.lru_cache(maxsize=64)
def degrade(w, h, cores):
    """Imagem w x h com degradê vertical passando pelas cores."""
    coluna = Image.new("RGB", (1, len(cores)))
    coluna.putdata([ImageColor.getrgb(c) for c in cores])
    return coluna.resize((max(1, w), max(1, h)), Image.BILINEAR)


class Pintor:
    """Desenha em coordenadas lógicas (as do layout) numa imagem SS vezes maior."""

    def __init__(self, w, h, escala, fundo=CHAVE, base=None):
        self.w, self.h, self.escala, self.e = w, h, escala, escala * SS
        tam = (round(w * self.e), round(h * self.e))
        self.img = base.copy() if base is not None else Image.new("RGB", tam, fundo)
        self.d = ImageDraw.Draw(self.img, "RGBA")  # "RGBA": cores com opacidade se misturam ao fundo

    def _pts(self, pontos):
        return [(x * self.e, y * self.e) for x, y in pontos]

    def _larg(self, larg):
        return max(1, round(larg * self.e))

    def poli(self, pontos, cor, borda=None, larg=1.0):
        self.d.polygon(self._pts(pontos), fill=cor, outline=borda, width=self._larg(larg) if borda else 0)

    def caixa(self, x0, y0, x1, y1, cor, sk=0.0, borda=None, larg=1.0):
        """Retângulo inclinado, como os painéis e botões do jogo."""
        self.poli([(x0 + sk, y0), (x1, y0), (x1 - sk, y1), (x0, y1)], cor, borda, larg)

    def circulo(self, cx, cy, r, cor=None, borda=None, larg=1.0):
        self.d.ellipse(self._pts([(cx - r, cy - r), (cx + r, cy + r)]), fill=cor, outline=borda,
                       width=self._larg(larg) if borda else 0)

    def arco(self, cx, cy, r, graus, cor, larg):
        """Arco de anel com pontas redondas, começando no topo e indo no sentido horário."""
        if graus <= 0:
            return
        meio = r + larg / 2  # o Pillow desenha a espessura para dentro da caixa
        self.d.arc(self._pts([(cx - meio, cy - meio), (cx + meio, cy + meio)]), -90, -90 + min(graus, 360),
                   fill=cor, width=self._larg(larg))
        if graus < 360:
            for g in (0, graus):
                a = math.radians(g - 90)
                self.circulo(cx + r * math.cos(a), cy + r * math.sin(a), larg / 2, cor)

    def texto(self, x, y, s, px, cor, borda=0.0, ancora="lm", frase=False):
        """Texto com contorno preto: legível em cima de qualquer cena do jogo."""
        self.d.text((x * self.e, y * self.e), s, font=fonte(px * self.e, frase), fill=cor, anchor=ancora,
                    stroke_width=round(borda * self.e), stroke_fill=alfa(PRETO, int(cor[7:] or "FF", 16) / 255))

    def largura(self, s, px, frase=False):
        return fonte(px * self.e, frase).getlength(s) / self.e

    def texto_ouro(self, x, y, s, px, borda, ancora="rs", cores=OURO):
        """Texto com degradê vertical e contorno preto, como os números do jogo."""
        f, xy = fonte(px * self.e), (x * self.e, y * self.e)
        self.d.text(xy, s, font=f, fill=PRETO, anchor=ancora, stroke_width=round(borda * self.e), stroke_fill=PRETO)
        x0, y0, x1, y1 = (int(v) for v in self.d.textbbox(xy, s, font=f, anchor=ancora))
        mascara = Image.new("L", (x1 - x0 + 1, y1 - y0 + 1), 0)
        ImageDraw.Draw(mascara).text((xy[0] - x0, xy[1] - y0), s, font=f, fill=255, anchor=ancora)
        self.img.paste(degrade(mascara.width, mascara.height, cores), (x0, y0), mascara)

    def bau(self, cx, cy, c):
        """Baú em pixel art centrado em (cx, cy), cada pixel com c de lado."""
        ox, oy = cx - 8 * c, cy - 8 * c
        for j, linha in enumerate(BAU):
            i = 0
            while i < 16:  # junta pixels iguais em faixas
                f = i
                while f < 16 and linha[f] == linha[i]:
                    f += 1
                if linha[i] != ".":
                    self.d.rectangle(self._pts([(ox + i * c, oy + j * c), (ox + f * c, oy + (j + 1) * c)]),
                                     fill=BAU_CORES[linha[i]])
                i = f

    def brilho(self, cx, cy, r, cor, forca):
        """Halo desfocado em volta de (cx, cy)."""
        R = r * self.e
        m = int(R * 1.7)
        halo = Image.new("RGBA", (2 * m, 2 * m), (0, 0, 0, 0))
        ImageDraw.Draw(halo).ellipse((m - R, m - R, m + R, m + R), outline=ImageColor.getrgb(cor) + (round(255 * forca),),
                                     width=max(2, round(R * 0.45)))
        halo = halo.filter(ImageFilter.GaussianBlur(R * 0.2))
        x, y = round(cx * self.e) - m, round(cy * self.e) - m
        self.img.paste(halo, (x, y), halo)

    def degrade_em(self, pontos, cores):
        """Preenche o polígono com degradê vertical."""
        pts = self._pts(pontos)
        x0, y0 = int(min(p[0] for p in pts)), int(min(p[1] for p in pts))
        x1, y1 = int(max(p[0] for p in pts)) + 1, int(max(p[1] for p in pts)) + 1
        mascara = Image.new("L", (x1 - x0, y1 - y0), 0)
        ImageDraw.Draw(mascara).polygon([(x - x0, y - y0) for x, y in pts], fill=255)
        self.img.paste(degrade(x1 - x0, y1 - y0, tuple(cores)), (x0, y0), mascara)

    def final(self):
        """Imagem no tamanho real. BOX não cria halos claros/escuros em volta da cor transparente."""
        return self.img.resize((round(self.w * self.escala), round(self.h * self.escala)), Image.BOX)


def ativar_dpi():
    """Faz o Windows entregar pixels reais em telas com escala (125%, 150%): sem isso a janela sai borrada."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            log.debug("Não deu para ativar DPI real; a janela pode ficar borrada")


def fator_dpi(root):
    """Escala do Windows (1.0 = 100%, 1.5 = 150%)."""
    return max(1.0, root.winfo_fpixels("1i") / 96)


# Fase do jogo no painel: texto e cor
FASES = {"lobby": ("NO LOBBY", CIANO), "fila": ("PROCURANDO PARTIDA", AMARELO), "partida": ("EM PARTIDA", VERDE),
         "fechado": ("JOGO FECHADO", CINZA), None: ("JOGO NÃO DETECTADO", CINZA)}

# Painel: tamanho lógico e inclinação; o anel do baú
PW, PH, PSK = 320, 188, 16
ANEL_X, ANEL_Y, ANEL_R, ANEL_L = 62, 46, 30, 7
CEL_Y0, CEL_Y1 = 94, 124  # cartões de estatística
BARRA_X0, BARRA_X1, BARRA_Y0, BARRA_Y1 = 24, 300, 145, 155


def celulas():
    """(x0, x1) dos 3 cartões de estatística."""
    return [(24 + i * 94, 112 + i * 94) for i in range(3)]


@functools.lru_cache(maxsize=4)
def base_painel(k, token):
    """Partes que não mudam: fundo, faixas, contorno, cartões vazios e rótulos fixos."""
    p = Pintor(PW, PH, k)
    forma = [(PSK, 0), (PW, 0), (PW - PSK, PH), (0, PH)]

    def faixa(y0, y1, cor, x0=0, x1=PW):
        p.poli([(x0 + PSK * (1 - y0 / PH), y0), (x1 - PSK * y0 / PH, y0),
                (x1 - PSK * y1 / PH, y1), (x0 + PSK * (1 - y1 / PH), y1)], cor)

    p.degrade_em(forma, (AZUL_TOPO, misturar(AZUL_TOPO, AZUL_NOITE, 0.55), AZUL_NOITE))
    faixa(0, PH * 0.4, alfa(BRANCO, 0.05))  # brilho de vidro em cima
    faixa(PH - 24, PH, AZUL_FAIXA)
    faixa(PH - 25, PH - 23, AMARELO)
    faixa(0, 3, CIANO)
    faixa(0, PH, AMARELO, x1=PSK + 10)
    p.poli(forma, None, PRETO, 2)

    for x0, x1 in celulas():
        p.caixa(x0, CEL_Y0, x1, CEL_Y1, alfa(AZUL_FAIXA, 0.55), sk=6, borda=BORDA, larg=1)
    for (x0, _), rotulo in zip(celulas()[1:], ("SESSÃO", "BAÚS/HORA")):
        p.texto(x0 + 12, CEL_Y0 + 9, rotulo, 10, CINZA)

    p.texto(24, 136, "TOKEN PIRATA", 12, BRANCO, 1)
    p.texto(24 + p.largura("TOKEN PIRATA", 12) + 7, 136.5, f"1 EM {token:,}".replace(",", "."), 10, CINZA)
    p.caixa(BARRA_X0, BARRA_Y0, BARRA_X1, BARRA_Y1, AZUL_FAIXA, sk=4, borda=PRETO, larg=1.5)
    p.texto(300, 82, "BAÚS ABERTOS", 11, CINZA, ancora="rm")
    return p.img


def quadro(cont, t, k, tecla, anim, atalhos, token, fase):
    """Imagem do painel no instante t, ou None se nada mudou desde o último quadro (economiza CPU)."""
    s = cont.segurando(t)
    if cont.total > anim["visto"]:
        anim["pop"] = t
    anim["visto"] = cont.total
    atual = fase.atual
    if atual != anim["fase"]:
        anim["fase"], anim["fase_t"] = atual, t
    p_token = chance_token(cont.total, token)
    anim["barra"] += (p_token - anim["barra"]) * 0.25
    ritmo = por_hora(cont.sessao, t - anim["inicio"])
    dt, dfase = t - anim["pop"], t - anim["fase_t"]

    animando = dt < 0.9 or dfase < 0.7 or s is not None or atual == "fila" or abs(p_token - anim["barra"]) > 1e-7
    chave = (cont.total, cont.sessao, cont.partida, cont.ultima, ritmo, atual, fase.partidas)
    if not animando and chave == anim["chave"]:
        return None
    anim["chave"] = chave

    p = Pintor(PW, PH, k, base=base_painel(k, token))
    cx, cy, r = ANEL_X, ANEL_Y, ANEL_R

    # baú entrou: raios dourados girando, halo e "+1" subindo
    if dt < 0.6:
        alcance, giro, a = r * (1.5 - 0.35 * dt / 0.6), dt * 90, 1 - dt / 0.6
        for n in range(10):
            g = math.radians(giro + n * 36)
            pontas = [(cx + alcance * math.cos(g + d), cy + alcance * math.sin(g + d)) for d in (-0.15, 0.15)]
            p.poli([(cx, cy), *pontas], alfa(AMARELO if n % 2 else "#FFB000", a))
    if dt < 0.5:
        p.brilho(cx, cy, r, AMARELO, 0.8 * (1 - dt / 0.5))

    # anel de interação (igual ao "segure E" do jogo)
    cor = None
    if s is not None:
        cor = VERMELHO if s > cont.maximo else VERDE if s >= cont.minimo else BRANCO
        if cor == VERDE:
            p.brilho(cx, cy, r, VERDE, 0.55 + 0.2 * math.sin(t * 12))
    p.circulo(cx, cy, r + ANEL_L / 2 + 1.5, PRETO)
    p.circulo(cx, cy, r - ANEL_L / 2, AZUL_NOITE)
    p.arco(cx, cy, r, 360, ANEL, ANEL_L)
    if cor:
        p.arco(cx, cy, r, 360 * min(s / cont.minimo, 1), cor, ANEL_L)
    p.bau(cx, cy - 4 * max(0.0, 1 - dt / 0.25), 3.3)

    # número grande com "pop" e o rótulo da fase em cima
    p.texto_ouro(300, 71, f"{cont.total:,}".replace(",", "."), 44 * (1 + 0.18 * max(0.0, 1 - dt / 0.3)), 3)
    if dt < 0.8:
        a = 1 - dt / 0.8
        p.texto(cx + r + 2, cy - r + 10 - 24 * dt, "+1", 22, alfa(VERDE, a), 2 * a, "mm")
    rotulo, cor_fase = FASES[atual]
    if atual == "partida":
        rotulo = f"{rotulo} {fase.partidas}"
    w = p.largura(rotulo, 12) + 30
    x1, x0, y0, y1 = 300, 300 - w, 10, 28
    if atual is None:
        p.caixa(x0, y0, x1, y1, None, sk=5, borda=CINZA, larg=1.2)
    else:
        p.caixa(x0, y0, x1, y1, cor_fase, sk=5, borda=PRETO, larg=1.2)
    pisca = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(t * 7)) if atual == "fila" else 1
    p.circulo(x0 + 13, 19, 3.5, alfa(CINZA if atual is None else AZUL_FAIXA, pisca))
    p.texto(x0 + 21, 19.5, rotulo, 12, CINZA if atual is None else AZUL_FAIXA)
    if dfase < 0.6:
        p.caixa(x0, y0, x1, y1, alfa(BRANCO, 0.7 * (1 - dfase / 0.6)), sk=5)

    # cartões: partida (ou partidas), sessão e ritmo
    em_partida = atual == "partida"
    valores = ((str(cont.partida), VERDE) if em_partida else ("--" if atual is None else str(fase.partidas), BRANCO),
               (f"+{cont.sessao}", VERDE), ("--" if ritmo is None else str(ritmo), CIANO))
    (x0, _) = celulas()[0]
    p.texto(x0 + 12, CEL_Y0 + 9, "NESTA PARTIDA" if em_partida else "PARTIDAS", 10, CINZA)
    for (x0, _), (valor, cor_valor) in zip(celulas(), valores):
        p.texto(x0 + 11, CEL_Y0 + 21.5, valor, 17, cor_valor, 1.5)

    # barra do token: enche com animação e um brilho passa quando entra baú
    p.texto(300, 136, texto_chance(p_token), 15, AMARELO, 1.5, "rm")
    if anim["barra"] > 0:
        fim = BARRA_X0 + 2 + max(7, (BARRA_X1 - BARRA_X0 - 4) * anim["barra"])
        p.caixa(BARRA_X0 + 2, BARRA_Y0 + 2, fim, BARRA_Y1 - 2, AMARELO, sk=3)
        p.caixa(BARRA_X0 + 2.5, BARRA_Y0 + 2, fim - 0.5, BARRA_Y0 + 4, alfa(BRANCO, 0.55), sk=1)
    if dt < 0.8:
        x = BARRA_X0 + (BARRA_X1 - BARRA_X0 + 30) * dt / 0.8
        p.caixa(max(BARRA_X0 + 2, x - 18), BARRA_Y0 + 2, min(BARRA_X1 - 2, x), BARRA_Y1 - 2,
                alfa(BRANCO, 0.3 * (1 - dt / 0.8)), sk=3)

    # rodapé: estado do hold, aviso de fora da partida ou atalhos
    if s is not None:
        rodape, cor_rod = f"SEGURANDO {tecla}   {s:.2f}s / {cont.minimo:.2f}s", cor
    elif not fase.pode_contar():
        rodape, cor_rod = f"FORA DA PARTIDA: SEGURAR {tecla} NÃO CONTA", CINZA
    elif cont.ultima is not None:
        rodape, cor_rod = f"ÚLTIMO HOLD: {cont.ultima:.2f}s", CINZA
    else:
        rodape, cor_rod = atalhos, CIANO
    p.texto(26, PH - 12, rodape, 12, cor_rod, 1)
    return p.final()


def colocar_icone(root):
    icone = RECURSOS / "assets" / "chesttracker.ico"
    if sys.platform == "win32" and icone.exists():
        root.iconbitmap(default=str(icone))


# Ajustes da janela: chave, rótulo, passo, mínimo, máximo, formato do valor
AJUSTES = (("min", "TEMPO MÍNIMO", 0.05, 0.1, 10, "{:.2f}s"), ("max", "TEMPO MÁXIMO", 0.1, 0.2, 30, "{:.1f}s"),
           ("escala", "TAMANHO DO PAINEL", 0.1, 0.3, 5, "{:.1f}x"), ("x", "POSIÇÃO X", 10, 0, 7680, "{:.0f} px"),
           ("y", "POSIÇÃO Y", 10, 0, 4320, "{:.0f} px"), ("token", "TOKEN PIRATA: 1 EM", 2500, 2500, 1000000, "{:,.0f}"))
CW, CH = 560, 700  # janela de configuração (tamanho lógico)


@functools.lru_cache(maxsize=2)
def base_config(f):
    """Fundo da janela de configuração: degradê, cabeçalho e títulos das seções."""
    p = Pintor(CW, CH, f)
    p.degrade_em([(0, 0), (CW, 0), (CW, CH), (0, CH)], (misturar(AZUL_TOPO, AZUL_NOITE, 0.35), AZUL_NOITE))
    p.caixa(0, 0, CW, 96, AZUL_FAIXA)
    p.caixa(0, 0, CW, 4, CIANO)
    p.caixa(0, 96, CW, 99, AMARELO)
    p.circulo(56, 50, 34, alfa(AZUL_TOPO, 0.35))
    p.bau(56, 50, 4)
    p.texto_ouro(104, 56, "CHESTTRACKER", 42, 3, "ls")
    p.texto(106, 77, "CONFIGURAÇÃO DO CONTADOR", 15, CIANO)

    for y, nome in ((116, "ATALHOS"), (398, "AJUSTES")):
        w = p.largura(nome, 16) + 34
        p.caixa(24, y, 24 + w, y + 26, AMARELO, sk=8, borda=PRETO, larg=1.2)
        p.texto(24 + 17, y + 13.5, nome, 16, PRETO)
    for n, (acao, rotulo) in enumerate(ACOES):
        y = 154 + n * 46
        p.caixa(24, y, CW - 24, y + 40, CARTAO, sk=10, borda=BORDA, larg=1)
        p.texto(46, y + 20.5, rotulo.upper(), 18, BRANCO)
    for n, (_, rotulo, *_resto) in enumerate(AJUSTES):
        x, y = 24 + (n % 3) * 174, 436 + (n // 3) * 70
        p.caixa(x, y, x + 162, y + 62, CARTAO, sk=10, borda=BORDA, larg=1)
        p.texto(x + 18, y + 15, rotulo, 12, CINZA)
    return p.img


def configurar(cfg, arquivo_cfg):
    """Tela antes do overlay, no estilo do jogo: atalhos e ajustes. Devolve a config escolhida ou None."""
    import queue
    import tkinter as tk
    from PIL import ImageTk
    from pynput import keyboard, mouse

    cfg = copia(cfg)  # só vale se clicar em INICIAR
    root = tk.Tk()
    root.title("ChestTracker")
    root.resizable(False, False)
    colocar_icone(root)
    f = fator_dpi(root)
    W, H = round(CW * f), round(CH * f)
    cv = tk.Canvas(root, width=W, height=H, bg=AZUL_NOITE, highlightthickness=0)
    cv.pack()
    foto = ImageTk.PhotoImage(Image.new("RGB", (W, H)))
    cv.create_image(0, 0, anchor="nw", image=foto)

    ui = {"hover": None, "alvo": None, "ouvintes": [], "erro": False, "fase": 0, "repetir": None,
          "status": "Clique num atalho e aperte a tecla. Use a rodinha do mouse nos ajustes."}
    fila, regioes = queue.Queue(), []

    def tecla(p, id_, x0, y0, x1, y1, cor, cor_hover, aresta, sk=8):
        """Botão com aresta mais escura embaixo, para parecer tecla; registra a área de clique."""
        sobre = ui["hover"] == id_
        p.caixa(x0, y0 + 4, x1, y1 + 4, aresta, sk=sk)
        p.caixa(x0, y0 + (1 if sobre else 0), x1, y1 + (1 if sobre else 0), cor_hover if sobre else cor, sk=sk)
        p.caixa(x0 + sk * 0.8, y0 + 1, x1 - 2, y0 + 3, alfa(BRANCO, 0.18), sk=1)
        regioes.append((x0, y0, x1, y1 + 4, id_))

    def quebrar(p, frase, px, largura):
        linhas, atual = [], ""
        for palavra in frase.split():
            tentativa = f"{atual} {palavra}".strip()
            if p.largura(tentativa, px, True) <= largura or not atual:
                atual = tentativa
            else:
                linhas.append(atual)
                atual = palavra
        return linhas + [atual]

    def render():
        regioes.clear()
        p = Pintor(CW, CH, f, base=base_config(f))
        for n, (acao, _) in enumerate(ACOES):
            y = 154 + n * 46
            for i in (0, 1):
                x0, id_ = 278 + i * 130, ("tecla", acao, i)
                nome = cfg["atalhos"][acao][i]
                if ui["alvo"] == (acao, i):
                    pulso = 0.5 + 0.5 * math.sin(time.monotonic() * 8)
                    p.caixa(x0 - 3, y + 2, x0 + 121, y + 38, None, sk=10, borda=alfa(AMARELO, 0.3 + 0.6 * pulso), larg=2)
                    tecla(p, id_, x0, y + 5, x0 + 118, y + 31, misturar("#FFB000", AMARELO, pulso),
                          AMARELO, "#8B5A12")
                    rotulo, cor_texto = "APERTE...", PRETO
                elif nome:
                    tecla(p, id_, x0, y + 5, x0 + 118, y + 31, TECLA, TECLA_HOVER, "#13307E")
                    rotulo, cor_texto = exibir(nome), BRANCO
                else:
                    tecla(p, id_, x0, y + 5, x0 + 118, y + 31, TECLA_VAZIA, BORDA, "#070E2C")
                    rotulo, cor_texto = "+ ADICIONAR", CINZA
                p.texto(x0 + 59, y + 18.5 + (1 if ui["hover"] == id_ else 0), rotulo, 16, cor_texto, ancora="mm")

        for n, (chave, _, _, _, _, fmt) in enumerate(AJUSTES):
            x, y, w = 24 + (n % 3) * 174, 436 + (n // 3) * 70, 162
            regioes.append((x, y, x + w, y + 62, ("campo", chave)))
            tecla(p, ("menos", chave), x + 12, y + 30, x + 44, y + 52, TECLA, TECLA_HOVER, "#13307E", sk=6)
            tecla(p, ("mais", chave), x + w - 44, y + 30, x + w - 12, y + 52, TECLA, TECLA_HOVER, "#13307E", sk=6)
            for cx in (x + 28, x + w - 28):  # sinais desenhados: nítidos em qualquer fonte
                dy = 1 if ui["hover"] in (("menos", chave), ("mais", chave)) and (cx < x + w / 2) == \
                    (ui["hover"][0] == "menos") else 0
                p.caixa(cx - 6, y + 39.5 + dy, cx + 6, y + 42.5 + dy, BRANCO)
                if cx > x + w / 2:
                    p.caixa(cx - 1.5, y + 35 + dy, cx + 1.5, y + 47 + dy, BRANCO)
            p.texto(x + w / 2, y + 41, fmt.format(cfg[chave]).replace(",", "."), 20, AMARELO, 1.5, "mm")

        cor = VERMELHO if ui["erro"] else BRANCO
        for i, linha in enumerate(quebrar(p, ui["status"], 13, CW - 70)[:2]):
            p.texto(44, 587 + i * 18, linha, 13, cor, frase=True)
        p.circulo(32, 588, 4, VERMELHO if ui["erro"] else CIANO)

        tecla(p, ("restaurar",), 24, 626, 200, 670, TECLA_VAZIA, BORDA, "#070E2C", sk=10)
        p.texto(112, 649, "RESTAURAR PADRÃO", 16, CINZA, ancora="mm")
        tecla(p, ("iniciar",), 216, 620, CW - 24, 670, AMARELO, "#FFE58A", "#B97C0E", sk=12)
        p.texto((216 + CW - 24) / 2, 646 + (1 if ui["hover"] == ("iniciar",) else 0), "INICIAR", 30, PRETO,
                ancora="mm")
        foto.paste(p.final())

    def avisar(msg, erro=False):
        ui.update(status=msg, erro=erro)
        render()

    def parar():
        for o in ui["ouvintes"]:
            o.stop()
        ui.update(alvo=None, ouvintes=[])

    def capturar(acao, i):
        parar()
        ui["alvo"] = (acao, i)
        avisar("Aperte a tecla ou o botão do mouse (meio, 4 ou 5). Esc cancela, Backspace limpa.")

        def clique(x, y, b, apertou):  # sem return: False pararia o listener
            if apertou:
                fila.put(nome_botao(b))

        k = keyboard.Listener(on_press=lambda key: fila.put(nome_tecla(key)))
        m = mouse.Listener(on_click=clique)
        k.start()
        m.start()
        ui["ouvintes"] = [k, m]

    def receber():
        while not fila.empty():
            nome = fila.get_nowait()
            if ui["alvo"] is None or nome is None:  # clique esquerdo/direito: continua esperando
                continue
            acao, i = ui["alvo"]
            parar()
            if nome == "esc":
                avisar("Cancelado.")
            elif nome == "backspace":
                cfg["atalhos"][acao][i] = ""
                avisar(f"{ROTULOS[acao]}: atalho removido.")
            else:
                avisar(atribuir(cfg["atalhos"], acao, i, nome))
        if ui["alvo"]:  # tecla em captura pulsa
            render()
        root.after(50, receber)

    def mudar(chave, sinal):
        _, _, passo, menor, maior, _ = next(a for a in AJUSTES if a[0] == chave)
        v = min(max(cfg[chave] + sinal * passo, menor), maior)
        cfg[chave] = round(v) if chave in ("x", "y", "token") else round(v, 2)
        ui["erro"] = False
        render()

    def segurar_mudar(id_, atraso=400):  # segurar o botão continua mudando
        ui["repetir"] = root.after(atraso, lambda: (mudar(id_[1], -1 if id_[0] == "menos" else 1),
                                                    segurar_mudar(id_, 60)))

    def achar(e):
        x, y = e.x / f, e.y / f
        return next((r[4] for r in reversed(regioes) if r[0] <= x <= r[2] and r[1] <= y <= r[3]), None)

    def mover(e):
        id_ = achar(e)
        id_ = None if id_ and id_[0] == "campo" else id_
        if id_ != ui["hover"]:
            ui["hover"] = id_
            cv.config(cursor="hand2" if id_ else "")
            render()

    def clicar(e):
        id_ = achar(e)
        if not id_:
            return
        if id_[0] == "tecla":
            capturar(id_[1], id_[2])
        elif id_[0] in ("menos", "mais"):
            mudar(id_[1], -1 if id_[0] == "menos" else 1)
            segurar_mudar(id_)
        elif id_[0] == "iniciar":
            iniciar()
        elif id_[0] == "restaurar":
            restaurar()

    def soltar(_e):
        if ui["repetir"]:
            root.after_cancel(ui["repetir"])
            ui["repetir"] = None

    def rodinha(e):
        id_ = achar(e)
        if id_ and id_[0] in ("campo", "menos", "mais"):
            mudar(id_[1], 1 if e.delta > 0 or getattr(e, "num", 0) == 4 else -1)

    resultado = {"cfg": None}

    def iniciar(_e=None):
        if ui["alvo"]:
            return  # Enter durante a captura vira atalho, não INICIAR
        erro = validar(cfg)
        if erro:
            avisar(erro, erro=True)
            return
        salvar_json(arquivo_cfg, cfg)
        resultado["cfg"] = copia(cfg)
        fechar()

    def restaurar():
        parar()
        cfg.clear()
        cfg.update(copia(PADRAO))
        avisar("Atalhos e ajustes voltaram ao padrão.")

    def fechar():
        parar()
        soltar(None)
        root.destroy()

    root.report_callback_exception = lambda *erro: log.error("Erro na janela de configuração", exc_info=erro)
    cv.bind("<Motion>", mover)
    cv.bind("<Button-1>", clicar)
    cv.bind("<ButtonRelease-1>", soltar)
    cv.bind("<MouseWheel>", rodinha)  # Windows
    cv.bind("<Button-4>", rodinha)    # Linux
    cv.bind("<Button-5>", rodinha)
    root.bind("<Return>", iniciar)
    root.protocol("WM_DELETE_WINDOW", fechar)
    render()
    receber()
    root.mainloop()
    return resultado["cfg"]


def novo_anim(cont, token, t):
    """Estado das animações do painel."""
    return {"visto": cont.total, "pop": float("-inf"), "inicio": t, "fase": "?", "fase_t": float("-inf"),
            "barra": chance_token(cont.total, token), "chave": None}


def overlay(cont, cfg, estado, fase):
    import tkinter as tk
    from PIL import ImageTk

    root = tk.Tk()
    root.title("ChestTracker")  # nome do botão na barra de tarefas
    colocar_icone(root)
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=CHAVE)
    if sys.platform == "win32":
        root.attributes("-transparentcolor", CHAVE)
    f = fator_dpi(root)
    k = cfg["escala"] * f  # mesmo tamanho na tela de antes, agora em pixels reais
    root.geometry(f"+{round(cfg['x'] * f)}+{round(cfg['y'] * f)}")
    W, H = round(PW * k), round(PH * k)
    cv = tk.Canvas(root, width=W, height=H, bg=CHAVE, highlightthickness=0)
    cv.pack()
    foto = ImageTk.PhotoImage(Image.new("RGB", (W, H), CHAVE))
    cv.create_image(0, 0, anchor="nw", image=foto)
    log.debug("Painel %dx%d (escala %.2f, DPI %.2f)", W, H, cfg["escala"], f)

    root.update_idletasks()
    hwnd = clique_atravessa(root) if sys.platform == "win32" else None
    root.report_callback_exception = lambda *erro: log.error("Erro no painel", exc_info=erro)

    tecla = exibir(next((n for n in cfg["atalhos"]["segurar"] if n), ""))
    atalhos = texto_atalhos(cfg["atalhos"])
    anim = novo_anim(cont, cfg["token"], time.monotonic())
    topo = {"t": 0.0}

    def tick():
        if estado["sair"]:
            root.destroy()
            return
        t = time.monotonic()
        try:
            img = quadro(cont, t, k, tecla, anim, atalhos, cfg["token"], fase)
            if img is not None:
                foto.paste(img)
        except Exception:  # um quadro com erro não pode parar o painel
            log.exception("Erro ao desenhar o painel")
        if hwnd and t - topo["t"] > 0.5:  # o jogo às vezes rouba o topo
            manter_no_topo(hwnd)
            topo["t"] = t
        root.after(33, tick)

    root.protocol("WM_DELETE_WINDOW", lambda: estado.update(sair=True))  # fechar pela barra de tarefas
    tick()
    root.mainloop()


def autoteste():
    c = Contador(1.0, 3.0)
    c.press(0); c.press(0.3); c.press(0.6)          # auto-repeat do Windows
    assert c.release(1.2) and c.total == 1 and c.sessao == 1  # baú aberto
    c.press(5); assert not c.release(5.4)            # toque curto (pegar item)
    c.press(10); assert not c.release(20)            # hold longo (reviver)
    assert not c.release(30)                         # release sem press
    assert c.segurando(31) is None
    c.press(40); assert abs(c.segurando(40.5) - 0.5) < 1e-9
    c.ajustar(-5); assert c.total == 0 and c.sessao == 0     # nunca negativo
    c.ajustar(+3); c.zerar(); assert c.total == 0 and c.sessao == 3  # F9 zera só o total
    assert len(BAU) == 16 and all(len(l) == 16 and set(l) <= set(BAU_CORES) | {"."} for l in BAU)
    assert misturar("#000000", "#FFFFFF", 0.5) == "#808080"
    assert por_hora(5, 60) is None                   # aquecimento
    assert por_hora(30, 1800) == 60 and por_hora(0, 600) == 0
    assert chance_token(0, 2000) == 0 and chance_token(5, 1) == 1
    assert abs(chance_token(2000, 2000) - 0.6322) < 1e-3       # 1 em N depois de N baús: ~63%
    assert texto_chance(0.39343) == "39,3%" and texto_chance(1) == "99,9%" and texto_chance(0) == "0,00%"
    assert texto_chance(chance_token(1000, 137500)) == "0,72%"

    # atalhos: segurar, auto-repeat, mouse, sair, tecla sem ação
    c, mudancas, saiu = Contador(0.85, 3.0, 10), [], []
    e = Entrada(c, copia(PADRAO["atalhos"]), lambda: mudancas.append(c.total), lambda: saiu.append(1))
    e.descer("e", 0); e.descer("e", 0.3); e.subir("e", 0.95)
    assert c.total == 11 and mudancas == [11]
    e.descer("mouse4", 1); e.descer("mouse4", 1.1); e.subir("mouse4", 1.2)
    assert c.total == 12                             # botão segurado não soma duas vezes
    e.descer("f7", 2); e.subir("f7", 2.1); assert c.total == 11
    e.descer("q", 3); e.descer(None, 3); e.subir("q", 3.1); assert c.total == 11 and not saiu
    e.subir("e", 4); assert c.total == 11            # soltar sem ter apertado
    e.descer("f10", 5); assert saiu == [1]
    c.nova_partida(); assert c.partida == 0 and c.total == 11

    # fase pelo log do Fortnite (linhas reais, encurtadas)
    f = Fase()
    assert f.atual is None and f.pode_contar()                  # sem log: conta normalmente
    eu, amigo = "MatchmakingLog: [2d63] gotoloucobr", "MatchmakingLog: [2d63] amigo"
    linhas = ["[1][808]LogOnlineAccount: Display: [x][process_user_login] Successfully logged in user. "
              "UserId=[1] DisplayName=[gotoloucobr] EpicAccountId=[MCP:1]",
              "[2][722]LogGlobalStatus: virtual UEngine::LoadMap Load map complete /Game/Maps/Frontend"]
    assert [f.linha(s) for s in linhas] == [False, False] and f.atual == "lobby" and not f.pode_contar()
    f.linha(f"[3][613]{amigo} ready status changed from Not Ready to Ready (None)")
    assert f.atual == "lobby"                                   # amigo pronto não é fila
    f.linha(f"[4][613]{eu} ready status changed from Not Ready to Ready (None)"); assert f.atual == "fila"
    f.linha(f"[5][989]{eu} ready status changed from Ready to Not Ready (CanceledUser)"); assert f.atual == "lobby"
    f.linha(f"[6][613]{eu} ready status changed from Not Ready to Ready (None)")
    assert f.linha("[7][1]LogGlobalStatus: virtual UEngine::LoadMap Load map complete /Delilah/Maps/Delilah_Terrain")
    assert f.atual == "partida" and f.pode_contar() and f.partidas == 1
    f.linha(f"[8][556]{eu} ready status changed from Ready to Not Ready (None)"); assert f.atual == "partida"
    f.linha(f"[9][1]{eu} ready status changed from Not Ready to Ready (None)"); assert f.atual == "fila"
    assert f.linha("[10][1]LogGlobalStatus: virtual UEngine::LoadMap Load map complete /Delilah/Maps/Delilah_Terrain")
    assert f.partidas == 2                                      # fila de dentro da partida para outra
    f.linha("[11][ 74]Log file closed, 11/08/26 12:35:42"); assert f.atual == "fechado" and not f.pode_contar()
    f.linha("[12][1]LogGlobalStatus: virtual UEngine::LoadMap Load map complete /Game/Maps/Frontend")
    f.linha("[13][200]LogExit: Game engine shut down"); assert f.atual == "fechado"
    c2 = Contador(0.85, 3.0)
    e2 = Entrada(c2, copia(PADRAO["atalhos"]), lambda: None, lambda: None, f.pode_contar)
    e2.descer("e", 0); e2.subir("e", 1); assert c2.total == 0  # jogo fechado: E não conta
    e2.descer("f8", 2); assert c2.total == 1                  # somar na mão sempre funciona
    f.linha("[14][1]LogGlobalStatus: virtual UEngine::LoadMap Load map complete /Delilah/Maps/Delilah_Terrain")
    f.linha("[15][50]LogExit: Name:IpNetDriver_2147482645 Def:BeaconNetDriver IpNetDriver_2147482645 shut down")
    assert f.atual == "partida" and f.pode_contar()             # conexão fechando no meio da partida

    at = copia(PADRAO["atalhos"])
    assert "Saiu de Somar +1" in atribuir(at, "descontar", 1, "f8")
    assert at["somar"] == ["", "mouse4"] and at["descontar"] == ["f7", "f8"]
    assert exibir("mouse4") == "MOUSE 4" and exibir("mouse4", True) == "M4"
    assert exibir("page_down") == "PAGE DOWN" and exibir("") == "—"
    assert texto_atalhos(PADRAO["atalhos"]) == "F8/M4 +1   F7 -1   F9 ZERA   F10 SAI"

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        arq = Path(d) / "config.json"
        assert carregar_config(arq) == PADRAO            # sem arquivo
        arq.write_text("{quebrado", encoding="utf-8"); assert carregar_config(arq) == PADRAO
        arq.write_text("[1, 2]", encoding="utf-8"); assert carregar_config(arq) == PADRAO
        bom = copia(PADRAO); bom["min"] = 0.9; bom["atalhos"]["segurar"] = ["f", "mouse5"]
        salvar_json(arq, bom); assert carregar_config(arq) == bom
        ruim = copia(PADRAO); ruim["min"] = 5.0          # mínimo maior que o máximo
        salvar_json(arq, ruim); assert carregar_config(arq) == PADRAO
        ruim = copia(PADRAO); ruim["token"] = 0          # dividiria por zero
        salvar_json(arq, ruim); assert carregar_config(arq) == PADRAO
        antigo = copia(PADRAO); del antigo["token"]      # config.json de versão anterior
        salvar_json(arq, antigo); assert carregar_config(arq)["token"] == PADRAO["token"]

        lg = Path(d) / "FortniteGame.log"
        leitor = LeitorLog(lg, bloco=8)
        assert leitor.novas() is None                    # jogo nunca aberto
        lg.write_bytes(b"\xef\xbb\xbfum\ndois\ntr")
        lidas = []
        while (n := leitor.novas()):
            lidas += n
        assert lidas == ["\ufeffum", "dois"]             # "tr" ainda está sendo escrita
        with open(lg, "ab") as arq_log:
            arq_log.write(b"es\n")
        assert leitor.novas() == ["tres"] and leitor.novas() == []
        lg.write_bytes(b"novo\n")                        # Fortnite abriu: log novo e menor
        assert leitor.novas() == ["novo"] and leitor.reiniciou
    sem_sair = copia(PADRAO); sem_sair["atalhos"]["sair"] = ["", ""]
    assert "Fechar contador" in validar(sem_sair) and validar(PADRAO) is None
    # desenho: painel em cada fase, quadro repetido não redesenha, janela de configuração
    t0 = 100.0
    c3, f3 = Contador(0.85, 3.0, 2689), Fase()
    anim = novo_anim(c3, 137500, t0)
    img = quadro(c3, t0, 1.0, "E", anim, texto_atalhos(PADRAO["atalhos"]), 137500, f3)
    assert img.size == (PW, PH) and img.getpixel((0, 0)) == ImageColor.getrgb(CHAVE)  # canto fica transparente
    assert quadro(c3, t0 + 1, 1.0, "E", anim, "", 137500, f3) is None
    for base, fila in (("lobby", False), ("lobby", True), ("partida", False), ("fechado", False)):
        f3.base, f3.fila = base, fila
        assert quadro(c3, t0 + 2, 1.5, "E", anim, "", 137500, f3).size == (480, 282)
    c3.press(t0 + 3)
    assert quadro(c3, t0 + 4, 1.0, "E", anim, "", 137500, f3) is not None  # segurando: anima
    assert base_config(1.0).size == (CW * SS, CH * SS)
    assert fonte(20).getlength("BAÚS") > 0 and alfa("#FFFFFF", 0.5) == "#FFFFFF80"
    # leitura das teclas sem gancho: códigos VK e bordas de descer/subir
    assert vk_de("e") == 0x45 and vk_de("7") == 0x37 and vk_de("f8") == 0x77 and vk_de("f24") == 0x87
    assert vk_de("mouse4") == 0x05 and vk_de("vk186") == 186 and vk_de("page_down") == 0x22
    assert vk_de(",") is None and vk_de(",", lambda c: 0x1BC) == 0xBC and vk_de("ç", lambda c: -1) is None
    c4, f4 = Contador(0.85, 3.0), Fase()
    e4 = Entrada(c4, copia(PADRAO["atalhos"]), lambda: None, lambda: None, f4.pode_contar)
    roteiro = iter([{0x45}] * 100 + [set()] * 2 + [{0x77}, set()])  # segura E 1 s, solta, toca F8
    estados, ciclos = [], iter(range(104))
    ler_teclas(e4, {"e", "f8", "q?"}, lambda vk: vk in estados[-1], intervalo=0,
               rodando=lambda: next(ciclos, None) is not None and not estados.append(next(roteiro)),
               relogio=lambda: len(estados) * 0.01)
    assert c4.total == 2  # E segurado (1 baú) + F8 (+1); "q?" sem código só gera aviso
    print("autoteste ok")


def configurar_log(debug, arquivo=PASTA / "chesttracker.log"):
    """Log na tela e em chesttracker.log (o .exe não tem console), com os erros que fecham o programa."""
    from logging.handlers import RotatingFileHandler
    handlers = [logging.StreamHandler()]
    try:
        handlers.append(RotatingFileHandler(arquivo, maxBytes=1_000_000, backupCount=1, encoding="utf-8"))
    except OSError as e:
        print(f"Sem log em arquivo ({e})", file=sys.stderr)
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    sys.excepthook = lambda *erro: log.critical("Erro que fechou o programa", exc_info=erro)
    threading.excepthook = lambda a: log.critical("Erro na thread %s", a.thread.name if a.thread else "?",
                                                  exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def main():
    p = argparse.ArgumentParser(description="ChestTracker: overlay contador de baús do Fortnite")
    p.add_argument("--arquivo", type=Path, default=PASTA / "baus.json",
                   help="onde salvar a contagem (padrão baus.json ao lado do script/.exe)")
    p.add_argument("--config", type=Path, default=PASTA / "config.json",
                   help="atalhos e ajustes (padrão config.json ao lado do script/.exe)")
    p.add_argument("--direto", action="store_true", help="pula a janela de configuração e usa o config.json")
    p.add_argument("--zerar", action="store_true", help="começa a contagem do 0")
    p.add_argument("--debug", action="store_true", help="log detalhado")
    p.add_argument("--autoteste", action="store_true", help="roda os testes da lógica e sai")
    args = p.parse_args()

    configurar_log(args.debug)

    if Image is None:
        sys.exit("Falta o Pillow: rode  pip install pillow")
    if args.autoteste:
        autoteste()
        return
    ativar_dpi()  # antes de abrir qualquer janela

    cfg = carregar_config(args.config)
    try:
        if not args.direto:
            cfg = configurar(cfg, args.config)
            if cfg is None:
                log.info("Janela de configuração fechada sem iniciar")
                return
        inicial = 0 if args.zerar else carregar(args.arquivo)
        cont = Contador(cfg["min"], cfg["max"], inicial)
        estado, fase = {"sair": False}, Fase()
        log.info("Iniciado com %d baús | conta entre %.2fs e %.2fs | atalhos: %s",
                 inicial, cfg["min"], cfg["max"], texto_atalhos(cfg["atalhos"]))
        iniciar_entrada(cont, cfg, args.arquivo, estado, fase)
        iniciar_fase(fase, cont)
    except ImportError:
        sys.exit("Falta o pynput: rode  pip install pynput")

    try:
        overlay(cont, cfg, estado, fase)
    except KeyboardInterrupt:
        pass
    finally:
        salvar(args.arquivo, cont.total)
        log.info("Encerrado com %d baús (salvo em %s)", cont.total, args.arquivo)


if __name__ == "__main__":
    main()
