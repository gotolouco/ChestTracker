#!/usr/bin/env python3
"""ChestTracker: overlay que conta os baús do Fortnite pelo tempo segurando a tecla de interação.

Abre uma janela para escolher atalhos e ajustes (salvos em config.json) e depois o overlay.
Requisitos: pip install pynput pillow   |   Fortnite em "Tela cheia em janela" (sem borda).
"""
import argparse
import contextlib
import ctypes
import faulthandler
import functools
import io
import json
import logging
import math
import os
import re
import struct
import sys
import threading
import time
import urllib.request
from pathlib import Path

try:
    from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter, ImageFont
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


def ler_titulo_spotify(titulo):
    """Título da janela do Spotify -> (artista, música).

    Tocando, o Spotify põe "Artista - Música" no título; pausado fica "Spotify", "Spotify Premium" ou
    "Spotify Free". O primeiro " - " separa: nomes de música têm " - " ("Song - Remastered"), artistas quase nunca.
    """
    artista, sep, musica = titulo.partition(" - ")
    artista, musica = artista.strip(), musica.strip()
    if not sep or not artista or not musica or artista.lower() == "spotify":  # pausado ou anúncio
        return None
    return artista, musica


_SPOTIFY = {"janelas": [], "nomes": {}, "nomes_t": float("-inf"), "varredura": float("-inf")}


def titulos_spotify(cache=_SPOTIFY, agora=None):
    """Títulos das janelas do Spotify.exe aberto (Windows). Só lê títulos: não usa rede nem login.

    As janelas do Spotify já achadas são relidas direto (barato). A varredura de todas as janelas do
    Windows só roda quando elas somem (no máximo a cada 5 s) ou a cada 30 s, para pegar janela nova.
    """
    from ctypes import wintypes
    u32, k32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                               ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    u32.IsWindow.argtypes = (wintypes.HWND,)
    u32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    u32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    u32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    agora = time.monotonic() if agora is None else agora
    if agora - cache["nomes_t"] > 60:  # nomes de processo por PID: refeito de vez em quando (PID é reaproveitado)
        cache["nomes"], cache["nomes_t"] = {}, agora
    nomes = cache["nomes"]

    def processo(pid):
        if pid not in nomes:
            nomes[pid] = None
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if h:
                try:
                    buf, n = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
                    if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                        nomes[pid] = Path(buf.value).name.lower()
                finally:
                    k32.CloseHandle(h)
        return nomes[pid]

    def cada(hwnd, _):
        try:  # primeiro o processo (não toca na janela); o título só das janelas do Spotify
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if processo(pid.value) == "spotify.exe" and u32.GetWindowTextLengthW(hwnd):
                achadas.append(hwnd)
        except Exception:  # exceção dentro do callback do Windows se perderia
            log.debug("Erro lendo janela %s", hwnd, exc_info=True)
        return True

    vivas = [h for h in cache["janelas"] if u32.IsWindow(h)]
    if (not vivas and agora - cache["varredura"] >= 5) or agora - cache["varredura"] >= 30:
        cache["varredura"], achadas = agora, []
        u32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cada), 0)
        vivas = achadas
    cache["janelas"] = vivas
    titulos = []
    for h in vivas:
        n = u32.GetWindowTextLengthW(h)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(h, buf, n + 1)
            titulos.append(buf.value)
    return titulos


def musica_spotify():
    """(artista, música) tocando agora no Spotify desktop, ou None (fechado, pausado ou anúncio)."""
    return next((m for m in map(ler_titulo_spotify, titulos_spotify()) if m), None)


def iniciar_spotify(estado, ler=musica_spotify, intervalo=1.0, medidor=None):
    """Acompanha a música do Spotify numa thread e guarda em estado["musica"]."""
    def rodar():
        falhou = False
        while True:
            try:
                t0 = time.perf_counter()
                atual, falhou = ler(), False
                if medidor is not None:
                    medidor.anotar("spotify (ler)", (time.perf_counter() - t0) * 1000)
            except Exception:
                if not falhou:  # avisa uma vez, não a cada segundo
                    log.exception("Erro ao ler o Spotify")
                atual, falhou = None, True
            if atual != estado["musica"]:
                log.debug("Spotify: %s", " - ".join(atual) if atual else "parado")
                estado["musica"] = atual
            time.sleep(intervalo)

    threading.Thread(target=rodar, name="spotify", daemon=True).start()


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
          "token": 137500,  # chance do token pirata: 1 em N baús
          "spotify": False,  # mostra a música do Spotify embaixo do painel (lê só o título da janela do Spotify)
          # rota: a rota de baús por cima do minimapa, acompanhando o personagem (seguir_mapa.py lê a tela).
          # "imagem": mapa de referência (baixado na página ROTA NO MAPA). "nome": rota desenhada no editor
          # (rotas/<nome>.json); vazio = linhas pintadas na própria imagem. "trocar": passa para a próxima rota.
          # "mostrar_baus": desenha também os baús conhecidos (baus_mapa.json). "espessura": 1 fina, 2 média, 3 grossa.
          "rota": {"ativo": False, "imagem": "mapa.jpg", "nome": "", "trocar": "f6", "mostrar_baus": False,
                   "espessura": 2,
                   # minimapa: x/y/lado = onde ele fica na tela (marque com --marcar-minimapa); escala 0 = descobrir.
                   # "confirmada": a escala já achou o jogador uma vez (aí quase não se procura outra).
                   "minimapa": {"x": None, "y": None, "lado": 0, "escala": 0.0, "hz": 4, "confirmada": False}}}
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


def nome_valido(nome):
    """Nome de rota vira nome de arquivo: letras, números, espaço, hífen e sublinhado, até 40 caracteres."""
    return bool(re.fullmatch(r"[\w\- ]{1,40}", nome)) and nome == nome.strip()


def ler_rota(dados):
    """Bloco "rota" do config.json por cima do padrão; valor inválido fica no padrão."""
    r = copia(PADRAO["rota"])
    if not isinstance(dados, dict):
        return r
    for chave in ("ativo", "mostrar_baus"):
        if isinstance(dados.get(chave), bool):
            r[chave] = dados[chave]
    for chave in ("imagem", "trocar"):
        if isinstance(dados.get(chave), str):
            r[chave] = dados[chave].strip() if chave == "imagem" else dados[chave].strip().lower()
    if isinstance(dados.get("nome"), str) and (not dados["nome"].strip() or nome_valido(dados["nome"].strip())):
        r["nome"] = dados["nome"].strip()
    v = dados.get("espessura")
    if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 3:
        r["espessura"] = v
    mini, m = dados.get("minimapa"), r["minimapa"]
    if isinstance(mini, dict):
        if isinstance(mini.get("confirmada"), bool):
            m["confirmada"] = mini["confirmada"]
        for chave, minimo, maximo, tipo in (("x", -20000, 20000, int), ("y", -20000, 20000, int),
                                            ("lado", 0, 4000, int), ("escala", 0, 20, float), ("hz", 1, 20, float)):
            v = mini.get(chave)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and minimo <= v <= maximo:
                m[chave] = tipo(v)
    return r


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
    if isinstance(dados.get("spotify"), bool):
        cfg["spotify"] = dados["spotify"]
    cfg["rota"] = ler_rota(dados.get("rota"))
    return cfg if validar(cfg) is None else copia(PADRAO)


def exibir(nome, curto=False):
    """Nome do atalho como aparece na tela: "e" -> "E", "mouse4" -> "MOUSE 4" (ou "M4")."""
    if not nome:
        return "—"
    if nome.startswith("mouse"):
        return ("M" if curto else "MOUSE ") + nome[5:]
    if nome in ("roda_cima", "roda_baixo"):
        return ("R" if curto else "RODA ") + ("↑" if nome == "roda_cima" else "↓")
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


def nome_rolagem(dy):
    """Giro da rodinha (dy do pynput ou do Windows) -> nome do atalho."""
    return "roda_cima" if dy > 0 else "roda_baixo" if dy < 0 else None


class Entrada:
    """Transforma tecla/botão apertado em ação do contador, conforme os atalhos.

    Teclas e rodinha chegam de threads diferentes: a trava evita que as duas mexam na contagem juntas.
    """

    def __init__(self, cont, atalhos, ao_mudar, ao_sair, pode_contar=lambda: True):
        self.cont, self.ao_mudar, self.ao_sair, self.pode_contar = cont, ao_mudar, ao_sair, pode_contar
        self.acoes = {n: acao for acao, nomes in atalhos.items() for n in nomes if n}
        self.apertadas = set()
        self._trava = threading.RLock()

    def descer(self, nome, t):
        with self._trava:
            self._descer(nome, t)

    def subir(self, nome, t):
        with self._trava:
            self._subir(nome, t)

    def _descer(self, nome, t):
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

    def _subir(self, nome, t):
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


ROLAGEM_PAUSA = 0.4  # segundos sem girar a rodinha = "soltou"


class RajadaRoda:
    """A rodinha não tem "apertado": cada giro é um evento solto. Giros seguidos (menos de ROLAGEM_PAUSA
    entre um e outro) viram uma rajada, que a Entrada recebe como tecla segurada do 1º ao último giro.
    Assim "vasculhar o baú girando a rodinha" passa pela mesma regra de tempo do E segurado."""

    def __init__(self, entrada, nomes, pausa=ROLAGEM_PAUSA):
        self.entrada, self.nomes, self.pausa = entrada, set(nomes), pausa
        self.ultimo = {}  # nome -> instante do último giro da rajada em curso
        self._trava = threading.Lock()

    def giro(self, nome, t):
        if nome not in self.nomes:
            return
        with self._trava:
            nova = nome not in self.ultimo
            self.ultimo[nome] = t
        if nova:
            self.entrada.descer(nome, t)

    def checar(self, t):
        """Fecha as rajadas que pararam há ROLAGEM_PAUSA: "solta" no instante do último giro."""
        with self._trava:
            acabaram = [(n, u) for n, u in self.ultimo.items() if t - u >= self.pausa]
            for n, _ in acabaram:
                del self.ultimo[n]
        for n, u in acabaram:
            self.entrada.subir(n, u)


def giros_do_lote(dados, n, cabecalho, extra=0):
    """Giros da rodinha num lote de RAWINPUT lido com GetRawInputBuffer.

    Cada bloco: RAWINPUTHEADER (dwType, dwSize, ...) e, se for mouse, RAWMOUSE (usButtonFlags no byte 4,
    usButtonData no 6). Os blocos começam alinhados em 8 bytes.
    """
    giros, pos = [], 0
    for _ in range(n):
        tipo, tam = struct.unpack_from("<II", dados, pos)
        if tipo == 0:  # RIM_TYPEMOUSE
            flags, delta = struct.unpack_from("<Hh", dados, pos + cabecalho + extra + 4)
            if flags & 0x0400 and delta:  # RI_MOUSE_WHEEL
                giros.append(nome_rolagem(delta))
        pos += (tam + 7) & ~7
    return giros


def ler_roda(rajada, rodando=lambda: True, intervalo=0.02):
    """Lê a rodinha do mouse por Raw Input (Windows) e passa os giros para a rajada.

    Raw Input não é gancho: o Windows põe uma cópia de cada evento do mouse numa fila deste programa e
    segue em frente, então o mouse e o jogo nunca esperam por nós. A fila é lida em lote a cada 20 ms
    (GetRawInputBuffer, como faz o SDL), o que custa pouco mesmo com mouse de 8000 Hz.
    """
    from ctypes import wintypes
    u32, k32 = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32")

    class Cabecalho(ctypes.Structure):  # RAWINPUTHEADER
        _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD), ("hDevice", wintypes.HANDLE),
                    ("wParam", wintypes.WPARAM)]

    class Dispositivo(ctypes.Structure):  # RAWINPUTDEVICE
        _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT), ("dwFlags", wintypes.DWORD),
                    ("hwndTarget", wintypes.HWND)]

    u32.CreateWindowExW.restype = wintypes.HWND
    u32.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                    wintypes.HINSTANCE, wintypes.LPVOID)
    u32.RegisterRawInputDevices.argtypes = (ctypes.POINTER(Dispositivo), wintypes.UINT, wintypes.UINT)
    u32.GetRawInputBuffer.restype = wintypes.UINT
    u32.GetRawInputBuffer.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.UINT), wintypes.UINT)
    u32.PeekMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                 wintypes.UINT)
    WM_INPUT, PM_REMOVE, RIDEV_INPUTSINK = 0x00FF, 0x0001, 0x00000100
    # janela invisível, só de mensagens, para receber a cópia dos eventos do mouse (com o jogo na frente)
    hwnd = u32.CreateWindowExW(0, "STATIC", "ChestTracker rodinha", 0, 0, 0, 0, 0, wintypes.HWND(-3), None, None, None)
    dispositivo = Dispositivo(0x01, 0x02, RIDEV_INPUTSINK, hwnd)  # página 1, uso 2 = mouse
    if not hwnd or not u32.RegisterRawInputDevices(ctypes.byref(dispositivo), 1, ctypes.sizeof(dispositivo)):
        log.error("Rodinha do mouse: o Windows não deixou ler (erro %d); use uma tecla", ctypes.get_last_error())
        return
    cabecalho, extra = ctypes.sizeof(Cabecalho), 0
    if ctypes.sizeof(ctypes.c_void_p) == 4:  # Python 32 bits num Windows 64: os dados vêm 8 bytes depois
        wow64 = wintypes.BOOL()
        k32.IsWow64Process(k32.GetCurrentProcess(), ctypes.byref(wow64))
        extra = 8 if wow64.value else 0
    buf, msg = ctypes.create_string_buffer(1 << 16), wintypes.MSG()
    log.info("Rodinha do mouse: lendo por Raw Input (%s)", ", ".join(exibir(n) for n in sorted(rajada.nomes)))
    while rodando():
        time.sleep(intervalo)
        while True:
            tam = wintypes.UINT(len(buf))
            n = u32.GetRawInputBuffer(buf, ctypes.byref(tam), cabecalho)
            if n in (0, 0xFFFFFFFF):
                break
            agora = time.monotonic()
            for nome in giros_do_lote(memoryview(buf), n, cabecalho, extra):
                rajada.giro(nome, agora)
        rajada.checar(time.monotonic())
        while u32.PeekMessageW(ctypes.byref(msg), None, 0, WM_INPUT - 1, PM_REMOVE):  # o resto da fila
            pass
        while u32.PeekMessageW(ctypes.byref(msg), None, WM_INPUT + 1, 0xFFFF, PM_REMOVE):
            pass


def testar_roda(segundos=8.0, arquivo=None):
    """Diagnóstico (--testar-roda): a leitura da rodinha funciona neste PC?

    Liga a leitura, simula 3 giros para baixo pelo Windows (SendInput) e escuta giros de verdade até o fim.
    Grava o resultado em roda_teste.txt e devolve True se os giros simulados chegaram.
    """
    from ctypes import wintypes
    eventos = []

    class Anota:
        def descer(self, nome, t):
            eventos.append(("começou", nome, t))

        def subir(self, nome, t):
            eventos.append(("parou", nome, t))

    rajada = RajadaRoda(Anota(), {"roda_cima", "roda_baixo"})
    giros = []
    rajada_giro = rajada.giro
    rajada.giro = lambda nome, t: (giros.append((nome, t)), rajada_giro(nome, t))
    t0 = time.monotonic()
    leitor = threading.Thread(target=ler_roda, args=(rajada, lambda: time.monotonic() - t0 < segundos), daemon=True)
    leitor.start()
    time.sleep(0.5)

    class Mouse(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class Entrada_(ctypes.Structure):  # INPUT com só o MOUSEINPUT (o maior da união)
        _fields_ = [("type", wintypes.DWORD), ("mi", Mouse)]

    u32 = ctypes.WinDLL("user32")
    u32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(Entrada_), ctypes.c_int)
    simulados = 0
    for _ in range(3):
        giro = Entrada_(0, Mouse(0, 0, ctypes.c_uint32(-120).value, 0x0800, 0, 0))  # MOUSEEVENTF_WHEEL, para baixo
        simulados += u32.SendInput(1, ctypes.byref(giro), ctypes.sizeof(giro))
        time.sleep(0.1)
    leitor.join(segundos + 2)
    rajada.checar(float("inf"))  # fecha a rajada que ainda estava girando quando o teste acabou
    chegaram = sum(1 for nome, t in giros if t - t0 < 1.5)
    linhas = [f"ChestTracker - teste da rodinha ({time.strftime('%Y-%m-%d %H:%M')}, {segundos:.0f} s)",
              f"Giros simulados pelo Windows: {simulados} enviados; giros lidos no 1º segundo e meio: {chegaram}",
              f"Giros lidos no total: {len(giros)} (↑ {sum(n == 'roda_cima' for n, _ in giros)}, "
              f"↓ {sum(n == 'roda_baixo' for n, _ in giros)})", "Rajadas:"]
    linhas += [f"  {quando:>8.2f} s  {o} ({exibir(n)})" for o, n, t in eventos for quando in [t - t0]]
    linhas.append("RESULTADO: " + ("FUNCIONA" if chegaram >= 3 else "NÃO LEU A RODINHA"))
    texto = "\n".join(linhas) + "\n"
    log.info("Teste da rodinha:\n%s", texto)
    try:
        (arquivo or PASTA / "roda_teste.txt").write_text(texto, encoding="utf-8")
    except OSError as e:
        log.error("Não deu para gravar roda_teste.txt (%s)", e)
    return chegaram >= 3


def iniciar_entrada(cont, cfg, arquivo, estado, fase):
    def mudou():
        log.info("Total %d", cont.total)
        salvar(arquivo, cont.total)

    e = Entrada(cont, cfg["atalhos"], mudou, lambda: estado.update(sair=True), fase.pode_contar)
    nomes = {n for ns in cfg["atalhos"].values() for n in ns if n}
    roda = {n for n in nomes if n in ("roda_cima", "roda_baixo")}
    rajada = RajadaRoda(e, roda) if roda else None  # sem rodinha nos atalhos: nem liga a leitura dela
    if sys.platform == "win32":
        get = ctypes.windll.user32.GetAsyncKeyState
        threading.Thread(target=ler_teclas, args=(e, nomes - roda, lambda vk: get(vk) & 0x8000), daemon=True).start()
        if rajada:
            threading.Thread(target=ler_roda, args=(rajada,), name="rodinha", daemon=True).start()
        return

    from pynput import keyboard, mouse  # fora do Windows: ganchos do pynput

    def clique(x, y, botao, apertou):  # sem return: False pararia o listener
        (e.descer if apertou else e.subir)(nome_botao(botao), time.monotonic())

    def rolou(x, y, dx, dy):
        if rajada:
            rajada.giro(nome_rolagem(dy), time.monotonic())

    def fechar_rajadas():
        while True:
            time.sleep(0.05)
            rajada.checar(time.monotonic())

    keyboard.Listener(on_press=lambda k: e.descer(nome_tecla(k), time.monotonic()),
                      on_release=lambda k: e.subir(nome_tecla(k), time.monotonic())).start()
    mouse.Listener(on_click=clique, on_scroll=rolou).start()  # listeners do pynput já são daemon
    if rajada:
        threading.Thread(target=fechar_rajadas, name="rodinha", daemon=True).start()


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

    def caber(self, s, px, largura, frase=False):
        """Corta s com "…" até caber na largura."""
        if self.largura(s, px, frase) <= largura:
            return s
        while s and self.largura(s + "…", px, frase) > largura:
            s = s[:-1]
        return s.rstrip() + "…" if s else ""

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
# Faixa do Spotify embaixo do painel; sem música essa área fica transparente
MUS_Y0, MUS_Y1, MUS_X1 = PH + 6, PH + 32, PW - PSK
ALTURA = MUS_Y1 + 1  # altura da janela: painel + faixa


def celulas():
    """(x0, x1) dos 3 cartões de estatística."""
    return [(24 + i * 94, 112 + i * 94) for i in range(3)]


@functools.lru_cache(maxsize=4)
def base_painel(k, token):
    """Partes que não mudam: fundo, faixas, contorno, cartões vazios e rótulos fixos."""
    p = Pintor(PW, ALTURA, k)
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


def desenhar_musica(p, musica, dmus):
    """Faixa do Spotify embaixo do painel, no mesmo estilo dele: degradê azul, faixa amarela à esquerda,
    risco ciano em cima, contorno preto e texto Anton como "TOKEN PIRATA  1 EM 137.500"."""
    artista, nome = musica
    sk = (MUS_Y1 - MUS_Y0) * PSK / PH  # mesma inclinação das laterais do painel
    forma = [(sk, MUS_Y0), (MUS_X1, MUS_Y0), (MUS_X1 - sk, MUS_Y1), (0, MUS_Y1)]
    p.degrade_em(forma, (AZUL_TOPO, misturar(AZUL_TOPO, AZUL_NOITE, 0.55), AZUL_NOITE))
    d = sk * 2 / (MUS_Y1 - MUS_Y0)  # risco ciano de 2 px seguindo as laterais
    p.poli([(sk, MUS_Y0), (MUS_X1, MUS_Y0), (MUS_X1 - d, MUS_Y0 + 2), (sk - d, MUS_Y0 + 2)], CIANO)
    p.caixa(0, MUS_Y0, 30, MUS_Y1, AMARELO, sk=sk)
    for i, h in enumerate((8, 14, 10)):  # barrinhas de som escuras no amarelo, como o ponto da fase
        x = 9 + i * 5
        p.caixa(x, MUS_Y1 - 6 - h, x + 3, MUS_Y1 - 6, AZUL_FAIXA)
    p.poli(forma, None, PRETO, 2)
    x, fim, y = 40, MUS_X1 - sk - 10, (MUS_Y0 + MUS_Y1) / 2
    nome = p.caber(nome.upper(), 12, fim - x)
    p.texto(x, y, nome, 12, BRANCO, 1)
    x += p.largura(nome, 12) + 7
    if fim - x > 30:  # sobra espaço: artista em cinza
        p.texto(x, y + 0.5, p.caber(artista.upper(), 10, fim - x), 10, CINZA)
    if dmus < 0.6:  # música nova: a faixa pisca, como o rótulo da fase
        p.poli(forma, alfa(BRANCO, 0.7 * (1 - dmus / 0.6)))


def quadro(cont, t, k, tecla, anim, atalhos, token, fase, musica=None):
    """Imagem do painel no instante t, ou None se nada mudou desde o último quadro (economiza CPU).

    musica: (artista, nome) tocando no Spotify, ou None para esconder a faixa.
    """
    s = cont.segurando(t)
    if cont.total > anim["visto"]:
        anim["pop"] = t
    anim["visto"] = cont.total
    atual = fase.atual
    if atual != anim["fase"]:
        anim["fase"], anim["fase_t"] = atual, t
    if musica != anim["musica"]:
        anim["musica"], anim["musica_t"] = musica, t
    p_token = chance_token(cont.total, token)
    anim["barra"] += (p_token - anim["barra"]) * 0.25
    ritmo = por_hora(cont.sessao, t - anim["inicio"])
    dt, dfase, dmus = t - anim["pop"], t - anim["fase_t"], t - anim["musica_t"]

    animando = dt < 0.9 or dfase < 0.7 or dmus < 0.7 or s is not None or atual == "fila" \
        or abs(p_token - anim["barra"]) > 1e-7
    chave = (cont.total, cont.sessao, cont.partida, cont.ultima, ritmo, atual, fase.partidas, musica)
    if not animando and chave == anim["chave"]:
        return None
    so_fila = atual == "fila" and chave == anim["chave"] and not (dt < 0.9 or dfase < 0.7 or dmus < 0.7 or s is not None
                                                                   or abs(p_token - anim["barra"]) > 1e-7)
    if so_fila and t - anim.get("desenhado", float("-inf")) < 0.1:
        return None  # só o pulso da fila: 10 quadros por segundo bastam
    anim["chave"], anim["desenhado"] = chave, t

    p = Pintor(PW, ALTURA, k, base=base_painel(k, token))
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
    if musica:
        desenhar_musica(p, musica, dmus)
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
# Página ROTA NO MAPA: os 4 passos (cartões largos) e as opções (grade 3x2, como os ajustes)
PASSOS_ROTA = ("BAIXAR MAPA E BAÚS", "DESENHAR A ROTA", "MARCAR O MINIMAPA")
OPCOES_ROTA = ("ROTA NO JOGO", "ROTA ATIVA", "BAÚS NO MINIMAPA", "TAMANHO DA ROTA", "TROCAR DE ROTA", "NOVA ROTA")
ESPESSURAS = {1: "FINA", 2: "MÉDIA", 3: "GROSSA"}
PASSO_Y, PASSO_H, OPCAO_Y = 154, 54, 388


@functools.lru_cache(maxsize=4)
def base_config(f, pagina="principal"):
    """Fundo da janela de configuração: degradê, cabeçalho e títulos das seções da página."""
    p = Pintor(CW, CH, f)
    p.degrade_em([(0, 0), (CW, 0), (CW, CH), (0, CH)], (misturar(AZUL_TOPO, AZUL_NOITE, 0.35), AZUL_NOITE))
    p.caixa(0, 0, CW, 96, AZUL_FAIXA)
    p.caixa(0, 0, CW, 4, CIANO)
    p.caixa(0, 96, CW, 99, AMARELO)
    p.circulo(56, 50, 34, alfa(AZUL_TOPO, 0.35))
    p.bau(56, 50, 4)
    p.texto_ouro(104, 56, "CHESTTRACKER", 42, 3, "ls")
    p.texto(106, 77, "CONFIGURAÇÃO DO CONTADOR" if pagina == "principal" else "ROTA NO MAPA", 15, CIANO)

    secoes = ((116, "ATALHOS"), (398, "AJUSTES")) if pagina == "principal" else ((116, "PASSO A PASSO"), (OPCAO_Y - 38, "OPÇÕES"))
    for y, nome in secoes:
        w = p.largura(nome, 16) + 34
        p.caixa(24, y, 24 + w, y + 26, AMARELO, sk=8, borda=PRETO, larg=1.2)
        p.texto(24 + 17, y + 13.5, nome, 16, PRETO)
    if pagina == "principal":
        for n, (acao, rotulo) in enumerate(ACOES):
            y = 154 + n * 46
            p.caixa(24, y, CW - 24, y + 40, CARTAO, sk=10, borda=BORDA, larg=1)
            p.texto(46, y + 20.5, rotulo.upper(), 18, BRANCO)
        grade = [rotulo for _, rotulo, *_resto in AJUSTES]
        gy = 436
    else:
        for n, titulo in enumerate(PASSOS_ROTA):
            y = PASSO_Y + n * (PASSO_H + 8)
            p.caixa(24, y, CW - 24, y + PASSO_H, CARTAO, sk=10, borda=BORDA, larg=1)
            p.texto(80, y + 18, titulo, 18, BRANCO)
        grade, gy = OPCOES_ROTA, OPCAO_Y
    for n, rotulo in enumerate(grade):
        x, y = 24 + (n % 3) * 174, gy + (n // 3) * 70
        p.caixa(x, y, x + 162, y + 62, CARTAO, sk=10, borda=BORDA, larg=1)
        p.texto(x + 18, y + 15, rotulo, 12, CINZA)
    return p.img


def configurar(cfg, arquivo_cfg, pagina="principal", aviso=None):
    """Tela antes do overlay, no estilo do jogo: atalhos, ajustes e a página ROTA NO MAPA.

    Devolve ("iniciar", cfg), (ferramenta, cfg, nome) para o main abrir o editor/ajuste e voltar, ou None.
    """
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
          "status": "Clique num atalho e aperte a tecla. Use a rodinha do mouse nos ajustes.",
          "pagina": pagina, "baixando": False, "info": None}
    fila, regioes, baixado = queue.Queue(), [], queue.Queue()

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

    def botao(p, id_, x0, y0, x1, y1, texto, ativo=True, destaque=False, px=16):
        """Tecla com texto; desligada fica apagada e não recebe clique."""
        if not ativo:
            p.caixa(x0, y0 + 4, x1, y1 + 4, "#070E2C", sk=8)
            p.caixa(x0, y0, x1, y1, TECLA_VAZIA, sk=8)
            cor = CINZA
        elif destaque:
            tecla(p, id_, x0, y0, x1, y1, AMARELO, "#FFE58A", "#B97C0E")
            cor = PRETO
        else:
            tecla(p, id_, x0, y0, x1, y1, TECLA, TECLA_HOVER, "#13307E")
            cor = BRANCO
        dy = 1 if ativo and ui["hover"] == id_ else 0
        p.texto((x0 + x1) / 2, (y0 + y1) / 2 + 0.5 + dy, texto, px, cor, ancora="mm")

    def seta(p, cx, cy, lado, cor):
        d = 5 * lado
        p.poli([(cx - d, cy), (cx + d, cy - 7), (cx + d, cy + 7)] if lado < 0 else
               [(cx + d, cy), (cx - d, cy - 7), (cx - d, cy + 7)], cor)

    def status(p):
        cor = VERMELHO if ui["erro"] else BRANCO
        for i, linha in enumerate(quebrar(p, ui["status"], 13, CW - 70)[:2]):
            p.texto(44, 587 + i * 18, linha, 13, cor, frase=True)
        pulso = 0.5 + 0.5 * math.sin(time.monotonic() * 8)
        p.circulo(32, 588, 4, VERMELHO if ui["erro"] else misturar(CIANO, AMARELO, pulso) if ui["baixando"] else CIANO)

    def render_rota(p):
        r, info, livre = cfg["rota"], ui["info"], not ui["baixando"]
        m, nomes, mapa = r["minimapa"], info["rotas"], info["mapa"]
        tem_rota = r["nome"] in nomes
        if info["mini"]:
            txt_mini = ("Baixe o mapa primeiro" if not mapa else "Marcado. Entre numa partida para ver" if m["lado"]
                        else "Numa partida, ponha o quadrado em cima do minimapa")
        else:
            txt_mini = ("Só na versão pelo código (não vem no .exe)" if getattr(sys, "frozen", False)
                        else "Instale o requirements-rota.txt para usar")
        passos = (  # (id, botão, pode clicar, feito, explicação)
            ("baixar", "ATUALIZAR" if mapa else "BAIXAR", livre, mapa,
             "Baixando..." if ui["baixando"] else f"Mapa baixado com {info['baus']} baús" if mapa
             else "Baixa o mapa e os baús do chestroute.com"),
            ("editar", "EDITAR", livre and mapa, tem_rota,
             f"Rota ativa: {r['nome']} ({len(nomes)} salvas)" if tem_rota else
             "Clique no mapa na ordem dos baús" if mapa else "Baixe o mapa primeiro"),
            ("marcar", "MARCAR", livre and mapa and info["mini"], bool(m["lado"]) and info["mini"], txt_mini))
        proximo = next((n for n, (_, _, pode, feito, _) in enumerate(passos) if pode and not feito), None)
        for n, (id_, rotulo, pode, feito, explica) in enumerate(passos):
            y = PASSO_Y + n * (PASSO_H + 8)
            p.circulo(52, y + PASSO_H / 2, 14, VERDE if feito else AMARELO if n == proximo else TECLA_VAZIA)
            p.texto(52, y + PASSO_H / 2 + 1, str(n + 1), 16, PRETO if feito or n == proximo else CINZA, ancora="mm")
            p.texto(80, y + 38, p.caber(explica, 12, CW - 24 - 140 - 80, frase=True), 12, CINZA, frase=True)
            botao(p, (id_,), CW - 150, y + 12, CW - 40, y + 38, rotulo, pode, n == proximo)

        def caixa_opcao(n):
            return 24 + (n % 3) * 174, OPCAO_Y + (n // 3) * 70, 162

        for n, valor, pode in ((0, r["ativo"], livre and mapa), (2, r["mostrar_baus"], livre and info["baus"] > 0)):
            x, y, w = caixa_opcao(n)
            botao(p, ("alternar", n), x + 12, y + 30, x + w - 12, y + 52,
                  "LIGADO" if valor else "DESLIGADO", pode, valor)
        x, y, w = caixa_opcao(1)  # rota ativa: setas para trocar
        varias = livre and len(nomes) > 1
        for lado, x0 in ((-1, x + 10), (1, x + w - 36)):
            id_ = ("rota", lado)
            if varias:
                tecla(p, id_, x0, y + 30, x0 + 26, y + 52, TECLA, TECLA_HOVER, "#13307E", sk=6)
            seta(p, x0 + 13, y + 41 + (1 if ui["hover"] == id_ else 0), lado, BRANCO if varias else BORDA)
        nome = p.caber(r["nome"].upper() if tem_rota else "NENHUMA", 14, w - 80)
        p.texto(x + w / 2, y + 41, nome, 14, AMARELO if tem_rota else CINZA, 1.2 if tem_rota else 0, "mm")
        x, y, w = caixa_opcao(3)  # espessura da linha no minimapa
        for lado, x0, sinal in ((-1, x + 12, "-"), (1, x + w - 44, "+")):
            botao(p, ("espessura", lado), x0, y + 30, x0 + 32, y + 52, sinal, livre and 1 <= r["espessura"] + lado <= 3,
                  px=18)
        p.texto(x + w / 2, y + 41, ESPESSURAS[r["espessura"]], 16, AMARELO, 1.2, "mm")
        x, y, w = caixa_opcao(4)  # tecla de trocar (do config.json)
        p.texto(x + w / 2, y + 41, exibir(r["trocar"]) if r["trocar"] else "-", 18, BRANCO, 1, "mm")
        x, y, w = caixa_opcao(5)
        botao(p, ("nova",), x + 12, y + 30, x + w - 12, y + 52, "+ CRIAR", livre and mapa)

        status(p)
        botao(p, ("pagina", "principal"), 24, 626, 200, 670, "VOLTAR", True)
        tecla(p, ("iniciar",), 216, 620, CW - 24, 670, AMARELO, "#FFE58A", "#B97C0E", sk=12)
        p.texto((216 + CW - 24) / 2, 646 + (1 if ui["hover"] == ("iniciar",) else 0), "INICIAR", 30, PRETO,
                ancora="mm")

    def render():
        regioes.clear()
        p = Pintor(CW, CH, f, base=base_config(f, ui["pagina"]))
        if ui["pagina"] == "rota":
            render_rota(p)
            foto.paste(p.final())
            return
        botao(p, ("pagina", "rota"), 380, 34, CW - 24, 62, "ROTA NO MAPA", True)
        p.texto(404, 411, "MÚSICA DO SPOTIFY", 12, CINZA, ancora="rm")  # ao lado de AJUSTES
        botao(p, ("spotify",), 414, 398, CW - 24, 422, "LIGADO" if cfg["spotify"] else "DESLIGADO", True,
              cfg["spotify"], px=14)
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

        status(p)

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
        avisar("Aperte a tecla, o botão do mouse (meio, 4 ou 5) ou gire a rodinha. Esc cancela, Backspace limpa.")

        def clique(x, y, b, apertou):  # sem return: False pararia o listener
            if apertou:
                fila.put(nome_botao(b))

        k = keyboard.Listener(on_press=lambda key: fila.put(nome_tecla(key)))
        m = mouse.Listener(on_click=clique, on_scroll=lambda x, y, dx, dy: fila.put(nome_rolagem(dy)))
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
        if not baixado.empty():
            msg, erro = baixado.get_nowait()
            ui["baixando"] = False
            recarregar_rota()
            avisar(msg, erro)
        if ui["alvo"] or ui["baixando"]:  # tecla em captura e download pulsam
            render()
        root.after(50, receber)

    def recarregar_rota():
        """As ferramentas gravam o bloco "rota" direto no config.json: a tela relê de lá."""
        cfg["rota"] = rota_do_arquivo(arquivo_cfg)
        ui["info"] = estado_rota(cfg["rota"])

    def abrir_pagina(nome):
        parar()
        ui["pagina"] = nome
        if nome == "rota":
            recarregar_rota()
            repetidos = atalhos_repetidos(cfg)
            if repetidos:
                avisar(f"{', '.join(exibir(n) for n in repetidos)} é atalho do contador e da rota ao mesmo tempo: "
                       "troque um deles.", erro=True)
                return
            avisar("Siga os passos na ordem. O passo amarelo é o próximo.")
        else:
            avisar("Clique num atalho e aperte a tecla. Use a rodinha do mouse nos ajustes.")

    def baixar():
        ui["baixando"] = True
        tinha_mapa = ui["info"]["mapa"]
        avisar("Baixando o mapa e os baús do chestroute.com. Pode levar um minuto...")

        def trabalho():
            msg, erro = baixar_mapa_e_baus(copia(cfg), arquivo_cfg)
            if tinha_mapa and not erro:
                msg += " Confira se a rota ainda bate com o mapa novo."
            baixado.put((msg, erro))

        threading.Thread(target=trabalho, name="baixar-mapa", daemon=True).start()

    def alternar(chave):
        r = cfg["rota"]
        campo = "ativo" if chave == 0 else "mostrar_baus"
        salvar_rota(arquivo_cfg, r, {campo: not r[campo]})
        recarregar_rota()
        render()

    def mudar_espessura(passo):
        nova = min(3, max(1, cfg["rota"]["espessura"] + passo))
        salvar_rota(arquivo_cfg, cfg["rota"], {"espessura": nova})
        recarregar_rota()
        avisar(f"Rota {ESPESSURAS[nova].lower()} no minimapa.")

    def trocar_rota(passo):
        nome = proxima_rota(ui["info"]["rotas"], cfg["rota"]["nome"], passo)
        salvar_rota(arquivo_cfg, cfg["rota"], {"nome": nome})
        recarregar_rota()
        avisar(f"Rota ativa: {nome}. No jogo, {exibir(cfg['rota']['trocar'])} também troca.")

    def ferramenta(acao, nome=""):
        """Fecha a janela e devolve ao main, que abre a ferramenta e reabre esta página depois."""
        resultado.update(acao=acao, nome=nome, cfg=copia(cfg))
        fechar()

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
        if ui["baixando"] and id_[0] not in ("tecla", "menos", "mais"):
            avisar("Espere o download terminar.")
            return
        if id_[0] == "tecla":
            capturar(id_[1], id_[2])
        elif id_[0] == "spotify":
            cfg["spotify"] = not cfg["spotify"]
            avisar("Música do Spotify ligada: aparece embaixo do painel enquanto o Spotify toca." if cfg["spotify"]
                   else "Música do Spotify desligada.")
        elif id_[0] == "pagina":
            abrir_pagina(id_[1])
        elif id_[0] == "baixar":
            baixar()
        elif id_[0] in ("editar", "nova"):
            r = cfg["rota"]
            nomes = ui["info"]["rotas"]
            ferramenta("editar", r["nome"] if id_[0] == "editar" and r["nome"] else nome_nova_rota(nomes))
        elif id_[0] == "marcar":
            ferramenta("marcar")
        elif id_[0] == "espessura":
            mudar_espessura(id_[1])
        elif id_[0] == "alternar":
            alternar(id_[1])
        elif id_[0] == "rota":
            trocar_rota(id_[1])
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
        if ui["alvo"]:
            return  # capturando atalho: o giro vira o atalho, não muda ajuste
        id_ = achar(e)
        if id_ and id_[0] in ("campo", "menos", "mais"):
            mudar(id_[1], 1 if e.delta > 0 or getattr(e, "num", 0) == 4 else -1)

    resultado = {"acao": None, "nome": "", "cfg": None}

    def iniciar(_e=None):
        if ui["alvo"]:
            return  # Enter durante a captura vira atalho, não INICIAR
        if ui["baixando"]:
            avisar("Espere o download terminar.")
            return
        erro = validar(cfg)
        if erro:
            avisar(erro, erro=True)
            return
        salvar_json(arquivo_cfg, cfg)
        resultado.update(acao="iniciar", cfg=copia(cfg))
        fechar()

    def restaurar():
        parar()
        rota = cfg["rota"]  # a rota tem página própria e não volta ao padrão junto
        cfg.clear()
        cfg.update(copia(PADRAO), rota=rota)
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
    if ui["pagina"] == "rota":
        recarregar_rota()
        ui["status"] = "Siga os passos na ordem. O passo amarelo é o próximo."
    if aviso:
        ui.update(status=aviso[0], erro=aviso[1])
    render()
    receber()
    root.mainloop()
    if resultado["acao"] is None:
        return None
    if resultado["acao"] == "iniciar":
        return "iniciar", resultado["cfg"]
    return resultado["acao"], resultado["cfg"], resultado["nome"]


def estilo_popup(janela):
    """Janela extra (rota): mouse atravessa, não pega foco e não aparece na barra de tarefas."""
    GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT = -20, 0x80000, 0x20
    WS_EX_TOOLWINDOW, WS_EX_APPWINDOW, WS_EX_NOACTIVATE = 0x80, 0x40000, 0x8000000
    u32 = ctypes.windll.user32
    hwnd = u32.GetParent(janela.winfo_id())
    estilo = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    estilo = (estilo | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW
    u32.SetWindowLongW(hwnd, GWL_EXSTYLE, estilo)
    return hwnd

# Rota de baús por cima do mapa do jogo
def extrair_rota(img):
    """Só as linhas amarelas e as setas brancas da imagem, com contorno preto; o resto vira CHAVE."""
    rgb = img.convert("RGB")
    r, g, b = rgb.split()

    def faixa(canal, teste):
        return canal.point([255 if teste(v) else 0 for v in range(256)])

    amarelo = ImageChops.multiply(ImageChops.multiply(faixa(r, lambda v: v > 205), faixa(g, lambda v: v > 195)),
                                  faixa(b, lambda v: v < 110))
    branco = ImageChops.multiply(ImageChops.multiply(faixa(r, lambda v: v > 228), faixa(g, lambda v: v > 228)),
                                 faixa(b, lambda v: v > 228))
    marca = ImageChops.lighter(amarelo, branco).filter(ImageFilter.MedianFilter(3))
    saida = Image.new("RGB", rgb.size, CHAVE)
    saida.paste((0, 0, 0), mask=marca.filter(ImageFilter.MaxFilter(5)))
    saida.paste(rgb.point([0 if v == 1 else v for v in range(256)] * 3), mask=marca)
    return saida


def imagem_rota(rota):
    """Imagem do mapa da rota, no tamanho original. Levanta OSError/ValueError se o arquivo faltar ou for inválido."""
    caminho = Path(rota["imagem"]).expanduser()
    return Image.open(caminho if caminho.is_absolute() else PASTA / caminho).convert("RGB")


PASTA_ROTAS = PASTA / "rotas"


def baixador_http(timeout=20):
    """Função url -> bytes (ou None no 404). Usa curl_cffi se houver (passa pelo Cloudflare), senão urllib."""
    try:
        from curl_cffi import requests as creq
    except ImportError:
        creq = None
    local = threading.local()

    def baixar(url):
        if creq is not None:
            if not hasattr(local, "s"):  # uma sessão por thread: curl_cffi não é thread-safe
                local.s = creq.Session(impersonate="chrome")
            r = local.s.get(url, timeout=timeout)
            if r.status_code == 404:
                return None
            if r.status_code != 200:
                raise OSError(f"HTTP {r.status_code}")
            return r.content
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0 Safari/537.36"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise OSError(f"HTTP {e.code}") from e

    return baixar


def baixar_mapa(modelo, zoom, destino, baixar=None, trabalhadores=6, avisar=print):
    """Baixa os ladrilhos de um mapa (modelo com {z}, {x} e {y}), junta numa imagem só e salva em `destino`.

    Ladrilho que não existe (404) fica com a cor do fundo; a imagem é cortada na área que tem ladrilhos.
    Devolve (largura, altura, ladrilhos, (ox, oy)), onde (ox, oy) é o canto da imagem em pixels do mapa
    completo nesse zoom. Levanta ValueError/OSError com mensagem pronta para mostrar.
    """
    from concurrent.futures import ThreadPoolExecutor
    if not modelo.startswith(("http://", "https://")) or not all(c in modelo for c in ("{z}", "{x}", "{y}")):
        raise ValueError("o endereço precisa começar com http(s):// e ter {z}, {x} e {y}")
    if not 0 <= zoom <= 5:
        raise ValueError("zoom precisa ficar entre 0 e 5 (o 5 já dá uma imagem de 8192 px)")
    baixar = baixar or baixador_http()
    n = 2 ** zoom
    avisar(f"Baixando até {n * n} ladrilhos no zoom {zoom}...")

    def um(xy):
        x, y = xy
        url = modelo.replace("{z}", str(zoom)).replace("{x}", str(x)).replace("{y}", str(y))
        for tentativa in (1, 2, 3):
            try:
                return xy, baixar(url)
            except Exception as e:  # rede/HTTP de qualquer backend: tenta de novo e depois desiste
                if tentativa == 3:
                    raise OSError(f"falha ao baixar {url}: {e}") from e
                time.sleep(tentativa)

    with ThreadPoolExecutor(max_workers=trabalhadores) as pool:
        achados = {xy: dados for xy, dados in pool.map(um, [(x, y) for x in range(n) for y in range(n)]) if dados}
    if not achados:
        raise OSError("nenhum ladrilho encontrado: confira o endereço e o zoom")
    try:
        primeiro = Image.open(io.BytesIO(next(iter(achados.values()))))
        lado = primeiro.width
        xs, ys = [x for x, _ in achados], [y for _, y in achados]
        x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
        mapa = Image.new("RGB", ((x1 - x0 + 1) * lado, (y1 - y0 + 1) * lado), AZUL_NOITE)
        for (x, y), dados in achados.items():
            ladrilho = Image.open(io.BytesIO(dados)).convert("RGB")
            if ladrilho.size != (lado, lado):
                ladrilho = ladrilho.resize((lado, lado), Image.LANCZOS)
            mapa.paste(ladrilho, ((x - x0) * lado, (y - y0) * lado))
    except OSError as e:  # inclui UnidentifiedImageError (ex.: Pillow sem WebP, ou página de bloqueio no lugar da imagem)
        raise OSError(f"um ladrilho não é uma imagem que o Pillow abre ({e}); atualize com: pip install -U pillow") from e
    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.suffix.lower() in (".jpg", ".jpeg"):
        mapa.save(destino, quality=92)
    else:
        mapa.save(destino)
    return mapa.width, mapa.height, len(achados), (x0 * lado, y0 * lado)


ARQ_BAUS = "baus_mapa.json"
COR_BAU = {"normal": "#FF9628", "buried": "#B07A4A", "rare": "#3FD0FF", "locked": "#3FD0FF", "vault": "#C77DFF"}


def ler_baus_site(site, versao=None, baixar=None):
    """Baús publicados por um site no formato do chestroute.com: (versão, [{"x", "y", "type", "prob"}, ...]).

    As coordenadas vêm em pixels do mapa no zoom 3. Levanta OSError/ValueError com mensagem pronta.
    """
    baixar = baixar or baixador_http()
    site = site.rstrip("/")

    def texto(url):
        dados = baixar(url)
        if dados is None:
            raise OSError(f"{url} não existe (404)")
        return dados.decode("utf-8", "replace")

    pagina = texto(site + "/")
    principal = re.search(r"/assets/index-[\w-]+\.js", pagina)
    if not principal:
        raise ValueError("não achei o script principal do site (o site mudou?)")
    codigo = texto(site + principal.group(0))
    arquivos = dict(re.findall(r"data/([\d.]+)/chests\.json.{0,40}?import\(\"\./(chests-[\w-]+\.js)\"", codigo))
    if not arquivos:
        raise ValueError("não achei a lista de baús no site (o site mudou?)")
    if versao not in arquivos:
        versao = max(arquivos, key=lambda v: [int(n) for n in v.split(".") if n.isdigit()])
    bruto = texto(f"{site}/assets/{arquivos[versao]}")
    m = re.search(r"JSON\.parse\('(.*)'\)", bruto, re.S)
    try:
        lista = json.loads(m.group(1).replace("\\'", "'") if m else bruto[bruto.index("["):bruto.rindex("]") + 1])
    except ValueError as e:
        raise ValueError(f"a lista de baús do site não é JSON válido ({e})") from e
    baus = [b for b in lista if isinstance(b, dict) and all(
        isinstance(b.get(k), (int, float)) and not isinstance(b.get(k), bool) for k in ("x", "y"))]
    if not baus:
        raise ValueError("a lista de baús veio vazia")
    return versao, baus


def salvar_baus(baus, zoom, origem, mapa, destino, fonte="", versao=""):
    """Converte os baús (pixels do zoom 3) para pixels da imagem do mapa e grava o arquivo. Devolve quantos."""
    f = 2.0 ** (zoom - 3)
    linhas = [[round(b["x"] * f - origem[0], 1), round(b["y"] * f - origem[1], 1), str(b.get("type", "normal")),
               float(b.get("prob", 1) or 0)] for b in baus]
    dados = {"mapa": mapa, "fonte": fonte, "versao": versao, "baus": linhas}
    destino.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
    return len(linhas)


def carregar_baus(rota, pasta=None):
    """Baús [(x, y, tipo, prob)] em pixels da imagem do mapa; [] se não há arquivo ou ele é de outro mapa."""
    try:
        dados = json.loads(((pasta or PASTA) / ARQ_BAUS).read_text(encoding="utf-8"))
        if dados.get("mapa") != rota["imagem"]:
            return []
        return [(float(x), float(y), str(t), float(p)) for x, y, t, p in dados["baus"]]
    except FileNotFoundError:
        return []
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        log.warning("Não deu para ler %s (%s)", ARQ_BAUS, e)
        return []


def desenhar_baus(img, baus, para_tela, r=4):
    """Marca cada baú com um quadradinho; `para_tela(x, y)` leva do mapa para a imagem. Baú incerto fica vazado."""
    d = ImageDraw.Draw(img)
    w, h = img.size
    for x, y, tipo, prob in baus:
        tx, ty = para_tela(x, y)
        if not (-r <= tx <= w + r and -r <= ty <= h + r):
            continue
        cor = COR_BAU.get(tipo, COR_BAU["normal"])
        d.rectangle((tx - r - 1, ty - r - 1, tx + r + 1, ty + r + 1), fill=PRETO)
        if prob >= 1:
            d.rectangle((tx - r + 1, ty - r + 1, tx + r - 1, ty + r - 1), fill=cor)
        else:
            d.rectangle((tx - r + 1, ty - r + 1, tx + r - 1, ty + r - 1), outline=cor, width=max(1, r // 3))
    return img


def bau_perto(baus, x, y, raio):
    """(x, y) do baú mais próximo dentro do raio (em pixels do mapa), ou None."""
    melhor = None
    for bx, by, _t, _p in baus:
        d = math.hypot(bx - x, by - y)
        if d <= raio and (melhor is None or d < melhor[0]):
            melhor = (d, bx, by)
    return None if melhor is None else (melhor[1], melhor[2])


def converter_rotas(de, para, fator, dx, dy, baus=(), pasta=None, raio_bau=14):
    """Passa para o mapa novo as rotas desenhadas sobre a imagem `de`: novo = antigo * fator + (dx, dy).

    Com `baus`, cada ponto gruda no baú que estiver a até `raio_bau` px. As originais vão para rotas/antigas.
    Devolve os nomes convertidos.
    """
    pasta = pasta or PASTA_ROTAS
    feitas = []
    for nome in listar_rotas(pasta):
        arquivo = pasta / f"{nome}.json"
        try:
            dados = json.loads(arquivo.read_text(encoding="utf-8"))
            if dados.get("mapa") != de:
                continue
            pontos = carregar_pontos(nome, pasta)
        except (OSError, ValueError, AttributeError) as e:
            log.warning("Rota %r não foi convertida (%s)", nome, e)
            continue
        (pasta / "antigas").mkdir(exist_ok=True)
        (pasta / "antigas" / f"{nome}.json").write_text(arquivo.read_text(encoding="utf-8"), encoding="utf-8")
        novos = [(x * fator + dx, y * fator + dy) for x, y in pontos]
        salvar_pontos(nome, [bau_perto(baus, x, y, raio_bau) or (x, y) for x, y in novos], para, pasta)
        feitas.append(nome)
    return feitas


def comando_baixar_mapa(cfg, arquivo_cfg, modelo, zoom, escala, baus_de="", converter=""):
    """--baixar-mapa: baixa o mapa para mapa.jpg, passa a usá-lo como fundo do editor e referência do minimapa.

    `baus_de`: site de onde vêm os baús (formato do chestroute.com), gravados em baus_mapa.json.
    `converter`: "imagem,fator,dx,dy" para trazer as rotas feitas sobre a imagem antiga (dx/dy no mapa completo).
    """
    destino = PASTA / "mapa.jpg"
    try:
        w, h, n, origem = baixar_mapa(modelo, zoom, destino)
    except (ValueError, OSError) as e:
        sys.exit(f"Não deu para baixar o mapa: {e}")
    print(f"Mapa salvo em {destino} ({w}x{h}, {n} ladrilhos).")
    (PASTA / "mapa.json").write_text(json.dumps({"modelo": modelo, "zoom": zoom, "origem": list(origem)}),
                                     encoding="utf-8")
    baus = []
    if baus_de:
        versao = re.search(r"/([\d]+\.[\d.]+)/", modelo)
        try:
            versao, lista = ler_baus_site(baus_de, versao.group(1) if versao else None)
            total = salvar_baus(lista, zoom, origem, destino.name, PASTA / ARQ_BAUS, baus_de, versao)
            baus = carregar_baus({"imagem": destino.name})
            print(f"{total} baús da versão {versao} salvos em {ARQ_BAUS}.")
        except (ValueError, OSError) as e:
            print(f"AVISO: o mapa baixou, mas os baús não: {e}")
    if converter:
        try:
            de, fator, dx, dy = converter.split(",")
            feitas = converter_rotas(de.strip(), destino.name, float(fator), float(dx) - origem[0],
                                     float(dy) - origem[1], baus)
            print(f"Rotas convertidas para o mapa novo: {', '.join(feitas) if feitas else 'nenhuma para converter'}.")
        except (ValueError, OSError) as e:
            print(f"AVISO: não deu para converter as rotas antigas: {e}")
    rota = cfg["rota"]
    # a escala do minimapa vale para a imagem antiga: recomeça do palpite
    salvar_rota(arquivo_cfg, rota, {"imagem": destino.name, "nome": rota["nome"] or "rota1", "ativo": True},
                {"escala": escala, "confirmada": False})
    print("Mapa ativado no config. Desenhe ou confira as rotas com editar_rota.bat.")


def listar_rotas(pasta=None):
    pasta = pasta or PASTA_ROTAS
    return sorted(a.stem for a in pasta.glob("*.json") if nome_valido(a.stem)) if pasta.is_dir() else []


def carregar_pontos(nome, pasta=None):
    """Pontos [(x, y)] da rota, em pixels da imagem do mapa. Levanta OSError/ValueError se faltar ou for inválida."""
    dados = json.loads(((pasta or PASTA_ROTAS) / f"{nome}.json").read_text(encoding="utf-8"))
    pontos = dados.get("pontos") if isinstance(dados, dict) else None
    if not isinstance(pontos, list) or not all(
            isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                                                        for v in p) for p in pontos):
        raise ValueError(f"rota {nome!r}: \"pontos\" precisa ser uma lista de [x, y]")
    return [(float(x), float(y)) for x, y in pontos]


def salvar_pontos(nome, pontos, mapa, pasta=None):
    pasta = pasta or PASTA_ROTAS
    pasta.mkdir(parents=True, exist_ok=True)
    dados = {"nome": nome, "mapa": mapa, "pontos": [[round(x, 1), round(y, 1)] for x, y in pontos]}
    (pasta / f"{nome}.json").write_text(json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")


def desenhar_pontos(img, pontos, esp=4, numeros=False):
    """Desenha a rota na imagem: linha amarela com contorno, seta no meio de cada trecho e um ponto por baú.

    O primeiro ponto é verde (começo). `numeros` escreve a ordem ao lado de cada ponto.
    """
    if not pontos:
        return img
    d = ImageDraw.Draw(img)
    pts = [(float(x), float(y)) for x, y in pontos]
    if len(pts) > 1:
        d.line(pts, fill=PRETO, width=esp + 4, joint="curve")
        d.line(pts, fill=AMARELO, width=esp, joint="curve")
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            comp = math.hypot(x1 - x0, y1 - y0)
            if comp < esp * 9:
                continue  # trecho curto: sem espaço para a seta
            ux, uy = (x1 - x0) / comp, (y1 - y0) / comp
            mx, my, t = (x0 + x1) / 2, (y0 + y1) / 2, esp * 2.2
            seta = [(mx + ux * t, my + uy * t), (mx - ux * t - uy * t, my - uy * t + ux * t),
                    (mx - ux * t + uy * t, my - uy * t - ux * t)]
            d.polygon(seta, fill=BRANCO, outline=PRETO)
    r = esp * 1.5
    for i, (x, y) in enumerate(pts):
        d.ellipse((x - r - 2, y - r - 2, x + r + 2, y + r + 2), fill=PRETO)
        d.ellipse((x - r, y - r, x + r, y + r), fill=VERDE if i == 0 else AMARELO)
        if numeros:
            d.text((x + r + 3, y - r - 3), str(i + 1), font=fonte(max(11, esp * 3)), fill=BRANCO,
                   stroke_width=2, stroke_fill=PRETO, anchor="ls")
    return img


def desenhar_rota_mini(img, pontos, esp=2):
    """Rota no minimapa, onde tudo é pequeno: linha fina, bolinha pequena por baú (sem empilhar as que
    caem quase no mesmo lugar), seta só em trecho comprido e o começo em verde."""
    if not pontos:
        return img
    d = ImageDraw.Draw(img)
    pts = [(float(x), float(y)) for x, y in pontos]
    if len(pts) > 1:
        d.line(pts, fill=PRETO, width=esp + 2, joint="curve")
        d.line(pts, fill=AMARELO, width=esp, joint="curve")
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            comp = math.hypot(x1 - x0, y1 - y0)
            if comp < 28:
                continue  # trecho curto: seta só atrapalha
            ux, uy = (x1 - x0) / comp, (y1 - y0) / comp
            mx, my, t = (x0 + x1) / 2, (y0 + y1) / 2, esp + 3
            d.polygon([(mx + ux * t, my + uy * t), (mx - ux * t - uy * t, my - uy * t + ux * t),
                       (mx - ux * t + uy * t, my - uy * t - ux * t)], fill=BRANCO, outline=PRETO)
    r, desenhados = esp * 0.5 + 1.5, []
    for i, (x, y) in enumerate(pts):
        if i and any(math.hypot(x - a, y - b) < 2 * r + 3 for a, b in desenhados):
            continue  # baú colado no anterior: uma bolinha basta
        desenhados.append((x, y))
        rr = r + 1 if i == 0 else r
        d.ellipse((x - rr - 1, y - rr - 1, x + rr + 1, y + rr + 1), fill=PRETO)
        d.ellipse((x - rr, y - rr, x + rr, y + rr), fill=VERDE if i == 0 else AMARELO)
    return img


class FonteRota:
    """A rota em uso: o mapa de referência e os pontos (ou, sem "nome", as linhas pintadas na própria imagem).

    pedir_troca vem da thread das teclas; aplicar, do Tk. `versao` muda quando a rota muda.
    """

    def __init__(self, rota, mapa=None, pasta=None):
        self.rota, self.pasta = rota, pasta
        if mapa is None:
            caminho = Path(rota["imagem"]).expanduser()
            mapa = Image.open(caminho if caminho.is_absolute() else PASTA / caminho)
        self.mapa = mapa.convert("RGB")
        self.nome, self.versao = rota["nome"], 0
        self.baus = carregar_baus(rota) if rota.get("mostrar_baus") else []
        self._lock, self._pedido = threading.Lock(), False
        self._carregar()

    def _carregar(self):
        self.pontos = self._legado = self._legado_grosso = None
        if not self.nome:
            self._legado = extrair_rota(self.mapa)
            self._legado_grosso = engrossar_rota(self._legado)
            return
        try:
            self.pontos = carregar_pontos(self.nome, self.pasta)
            log.info("Rota %r: %d pontos", self.nome, len(self.pontos))
        except (OSError, ValueError) as e:
            self.pontos = []
            log.error("Rota %r não abriu (%s); crie com editar_rota.bat", self.nome, e)

    def pedir_troca(self):
        with self._lock:
            self._pedido = True

    def aplicar(self):
        """Troca para a próxima rota da pasta, se as teclas pediram. Devolve True se trocou."""
        with self._lock:
            pedido, self._pedido = self._pedido, False
        nomes = listar_rotas(self.pasta)
        if not pedido or not nomes:
            return False
        self.nome = nomes[(nomes.index(self.nome) + 1) % len(nomes)] if self.nome in nomes else nomes[0]
        self._carregar()
        self.versao += 1
        return True

    def mini(self, x, y, escala, lado):
        """Pedaço da rota em volta do jogador (x, y no mapa), no tamanho do minimapa."""
        if self.pontos is None:
            return recorte_minimapa(self._legado_grosso, x, y, escala, lado)
        meio = lado * escala / 2
        img = Image.new("RGB", (lado, lado), CHAVE)
        desenhar_baus(img, self.baus, lambda bx, by: ((bx - x + meio) / escala, (by - y + meio) / escala), 2)
        return desenhar_rota_mini(img, [((px - x + meio) / escala, (py - y + meio) / escala) for px, py in self.pontos],
                                  self.rota.get("espessura", 2))


def abrir_fonte(rota):
    """FonteRota pronta, ou None se a rota está desligada ou a imagem do mapa não abre."""
    if not rota["ativo"]:
        return None
    try:
        return FonteRota(rota)
    except (OSError, ValueError) as e:
        log.error("Rota desligada: não deu para abrir o mapa %r (%s)", rota["imagem"], e)
        return None


class TeclasRota:
    """Faz o papel da Entrada no ler_teclas: a tecla `trocar` passa para a próxima rota."""

    def __init__(self, trocar, fonte_rota):
        self.trocar, self.fonte_rota = trocar, fonte_rota

    def descer(self, nome, t):
        if nome == self.trocar:
            self.fonte_rota.pedir_troca()

    def subir(self, nome, t):
        pass


def iniciar_teclas_rota(rota, fonte_rota):
    """Lê a tecla de trocar de rota numa thread (só sinaliza; quem troca é o Tk, em FonteRota.aplicar)."""
    if fonte_rota is None or not rota["trocar"] or sys.platform != "win32":
        return
    get = ctypes.windll.user32.GetAsyncKeyState
    threading.Thread(target=ler_teclas, args=(TeclasRota(rota["trocar"], fonte_rota), {rota["trocar"]},
                                              lambda vk: get(vk) & 0x8000), name="teclas-rota", daemon=True).start()


def engrossar_rota(linhas):
    """Linhas mais grossas, para continuarem visíveis depois de reduzidas ao tamanho do minimapa."""
    marca = Image.merge("RGB", linhas.split()).convert("L").point([255 if v > 1 else 0 for v in range(256)])
    marca = ImageChops.lighter(marca, linhas.getchannel("R").point([255 if v > 1 else 0 for v in range(256)]))
    grossa = marca.filter(ImageFilter.MaxFilter(5))
    saida = Image.new("RGB", linhas.size, CHAVE)
    saida.paste((0, 0, 0), mask=grossa.filter(ImageFilter.MaxFilter(7)))
    saida.paste(linhas.filter(ImageFilter.MaxFilter(5)), mask=grossa)
    return saida


def recorte_minimapa(linhas, x, y, escala, lado):
    """Pedaço da rota em volta do jogador (x, y na referência), no tamanho do minimapa; fora da imagem = CHAVE."""
    meio = lado * escala / 2
    x0, y0, x1, y1 = round(x - meio), round(y - meio), round(x + meio), round(y + meio)
    tela = Image.new("RGB", (max(1, x1 - x0), max(1, y1 - y0)), CHAVE)
    cx0, cy0, cx1, cy1 = max(x0, 0), max(y0, 0), min(x1, linhas.width), min(y1, linhas.height)
    if cx1 > cx0 and cy1 > cy0:
        tela.paste(linhas.crop((cx0, cy0, cx1, cy1)), (cx0 - x0, cy0 - y0))
    return tela.resize((lado, lado), Image.NEAREST)


class TelaMiniRota:
    """Rota por cima do minimapa, andando junto com o jogador. Só aparece enquanto o rastreador sabe a posição."""

    def __init__(self, root, rota, rastreador, fonte_rota, ao_achar_escala=None):
        import tkinter as tk
        from PIL import ImageTk
        m = rota["minimapa"]
        self.lado, self.rastreador, self.fonte_rota = m["lado"], rastreador, fonte_rota
        self.ao_achar_escala, self._escala_salva = ao_achar_escala, m["escala"] if m["confirmada"] else None
        jan = tk.Toplevel(root)
        jan.overrideredirect(True)
        jan.attributes("-topmost", True)
        jan.configure(bg=CHAVE)
        if sys.platform == "win32":
            jan.attributes("-transparentcolor", CHAVE)
        jan.geometry(f"{self.lado}x{self.lado}+{m['x']}+{m['y']}")
        cv = tk.Canvas(jan, width=self.lado, height=self.lado, bg=CHAVE, highlightthickness=0)
        cv.pack()
        self._foto = ImageTk.PhotoImage(Image.new("RGB", (self.lado, self.lado), CHAVE))
        cv.create_image(0, 0, anchor="nw", image=self._foto)
        jan.update_idletasks()
        self.jan, self._hwnd, self._ultima, self._topo = jan, None, None, 0.0
        if sys.platform == "win32":
            self._hwnd = estilo_popup(jan)
            # fora da captura de tela: senão o rastreador leria a própria rota desenhada no minimapa
            if not ctypes.windll.user32.SetWindowDisplayAffinity(self._hwnd, 0x11):
                # sem a exclusão, a rota desenhada em cima do minimapa enganaria a leitura: vai para o lado
                ao_lado = m["x"] - self.lado - 8 if m["x"] - self.lado - 8 >= 0 else m["x"] + self.lado + 8
                jan.geometry(f"+{ao_lado}+{m['y']}")
                log.warning("Minimapa: este Windows não deixa esconder a janela da captura; "
                            "a rota vai aparecer ao lado do minimapa, não em cima")

    def atualizar(self, t):
        pos = self.rastreador.posicao()
        chave = None if pos is None else (round(pos[0]), round(pos[1]), round(pos[2], 2), self.fonte_rota.versao)
        if pos is not None and self.ao_achar_escala and (self._escala_salva is None or
                                                         abs(pos[2] - self._escala_salva) > 0.05):
            self._escala_salva = pos[2]  # escala que achou o jogador (nova ou ainda não confirmada): guarda
            try:
                self.ao_achar_escala(round(pos[2], 2))
            except OSError as e:
                log.warning("Minimapa: não deu para salvar a escala no config (%s)", e)
        if chave != self._ultima:
            self._ultima = chave
            img = Image.new("RGB", (self.lado, self.lado), CHAVE) if chave is None else \
                self.fonte_rota.mini(pos[0], pos[1], pos[2], self.lado)
            self._foto.paste(img)
        if self._hwnd and t - self._topo > 0.5:
            manter_no_topo(self._hwnd)
            self._topo = t


def jogo_na_frente():
    """True se a janela em primeiro plano é o Fortnite (fora do Windows não dá para saber: True)."""
    if sys.platform != "win32":
        return True
    u32 = ctypes.windll.user32
    titulo = ctypes.create_unicode_buffer(128)
    u32.GetWindowTextW(u32.GetForegroundWindow(), titulo, 128)
    return "fortnite" in titulo.value.lower()


def iniciar_minirota(root, rota, fonte_rota, ativo=None, ao_achar_escala=None):
    """TelaMiniRota com o rastreador rodando, ou None se desligada/incompleta (o contador segue sem ela).

    `ativo()` diz quando ler o minimapa; fora disso a rota some e nada é capturado.
    """
    m = rota["minimapa"]
    if fonte_rota is None:
        return None
    if m["x"] is None or m["y"] is None or m["lado"] < 60:
        log.error("Rota no minimapa desligada: marque onde fica o minimapa com marcar_minimapa.bat")
        return None
    try:
        import numpy as np
        from seguir_mapa import Rastreador, Referencia
    except ImportError as e:
        log.error("Rota no minimapa desligada: falta %s (pip install -r requirements-rota.txt)", e.name or e)
        return None
    ref = Referencia(np.ascontiguousarray(np.asarray(fonte_rota.mapa)[:, :, ::-1]))
    # a escala do config é só o ponto de partida: se o mapa mudar, o rastreador procura a nova
    rast = Rastreador(ref, (m["x"], m["y"], m["lado"]), m["escala"], m["hz"], ativo=ativo, escala_fixa=False,
                      confirmada=m["confirmada"])
    tela = TelaMiniRota(root, rota, rast, fonte_rota, ao_achar_escala)
    rast.iniciar()
    return tela


def vista_da_rota(pontos, mapa_wh, tela_wh, margem=80):
    """(zoom, ox, oy) para a rota inteira caber na tela com folga; sem rota (ou 1 ponto), o mapa inteiro."""
    (mw, mh), (cw, ch) = mapa_wh, tela_wh
    z_min = min(cw / mw, ch / mh)
    if len(pontos) < 2:
        return z_min, -(cw / z_min - mw) / 2, -(ch / z_min - mh) / 2
    xs, ys = [x for x, _ in pontos], [y for _, y in pontos]
    x0, x1, y0, y1 = min(xs) - margem, max(xs) + margem, min(ys) - margem, max(ys) + margem
    z = min(8.0, max(z_min, min(cw / (x1 - x0), ch / (y1 - y0))))
    return z, (x0 + x1) / 2 - cw / z / 2, (y0 + y1) / 2 - ch / z / 2


def trecho_mais_perto(pts, x, y):
    """(i, distância) do trecho pts[i]→pts[i+1] mais perto de (x, y), ou None se não há trecho."""
    melhor = None
    for i, ((x0, y0), (x1, y1)) in enumerate(zip(pts, pts[1:])):
        dx, dy = x1 - x0, y1 - y0
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / (dx * dx + dy * dy)))
        d = math.hypot(x - (x0 + t * dx), y - (y0 + t * dy))
        if melhor is None or d < melhor[1]:
            melhor = (i, d)
    return melhor


def rotulos_sem_empilhar(pts, distancia=18):
    """Índices dos pontos que ganham número sem cobrir o número de outro (o primeiro e o último sempre)."""
    escolhidos = []
    for i in [0, len(pts) - 1] + list(range(1, len(pts) - 1)):
        if 0 <= i < len(pts) and i not in escolhidos and \
                all(math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1]) >= distancia for j in escolhidos):
            escolhidos.append(i)
    return sorted(escolhidos)


def pontos_nos_baus(pontos, baus):
    """Quantos pontos da rota estão em cima de um baú (grudaram no editor)."""
    return sum(1 for x, y in pontos if bau_perto(baus, x, y, 0.6))


EDITOR_AJUDA = (("CLIQUE", "novo ponto no fim"), ("CTRL+CLIQUE", "ponto no meio da rota"),
                ("ARRASTAR", "mover um ponto"), ("DIREITO", "apagar um ponto"), ("SHIFT", "ponto fora do baú"),
                ("RODINHA", "zoom"), ("MEIO / SETAS", "andar pelo mapa"), ("CTRL+Z", "desfazer"))
LADO_EDITOR, TOPO_EDITOR = 270, 64  # painel lateral e cabeçalho do editor (tamanho lógico)


def editar_rota(cfg, arquivo_cfg, nome):
    """Editor de rota no visual do programa: mapa à esquerda, painel com botões e ajuda à direita.

    Salva em rotas/<nome>.json e ativa no config. Fala por print (a página de rota mostra a mensagem).
    """
    import tkinter as tk
    from PIL import ImageTk
    rota = cfg["rota"]
    if not nome_valido(nome):
        sys.exit("Nome de rota inválido: use letras, números, espaço, - e _ (até 40 caracteres).")
    try:
        mapa = imagem_rota(rota)
    except (OSError, ValueError) as e:
        sys.exit(f"Não deu para abrir a imagem do mapa {rota['imagem']!r}: {e}")
    baus = carregar_baus(rota)
    try:
        pontos = carregar_pontos(nome)
    except FileNotFoundError:
        pontos = []
    except (OSError, ValueError) as e:
        sys.exit(f"A rota {nome!r} existe mas não abre: {e}")

    root = tk.Tk()
    root.title(f"ChestTracker - editor de rota: {nome}")
    root.configure(bg=AZUL_NOITE)
    f = fator_dpi(root)
    lado, topo = round(LADO_EDITOR * f), round(TOPO_EDITOR * f)
    W = max(900, round(root.winfo_screenwidth() * 0.9))
    H = max(640, round(root.winfo_screenheight() * 0.86))
    cw, ch = W - lado, H - topo                        # área do mapa, em pixels da tela
    hs = (H - topo) / f                                # altura lógica do painel
    colocar_icone(root)
    root.geometry(f"{W}x{H}+{(root.winfo_screenwidth() - W) // 2}+{max(0, (root.winfo_screenheight() - H) // 3)}")
    root.resizable(False, False)
    tela = tk.Canvas(root, width=W, height=H, bg=AZUL_NOITE, highlightthickness=0)
    tela.pack()

    z_min = min(cw / mapa.width, ch / mapa.height)
    z, ox, oy = vista_da_rota(pontos, mapa.size, (cw, ch))
    est = {"z": z, "ox": ox, "oy": oy, "pega": None, "pan": None, "desfazer": [], "salvo": False, "mudou": False,
           "mouse": None, "ctrl": False, "shift": False, "grudar": True, "ver_baus": bool(baus), "hover": None,
           "status": "Clique no mapa na ordem em que você pega os baús.", "erro": False, "limpar": False,
           "sair": False, "base": None, "base_chave": None}
    regioes, fotos = [], {}

    # ---------- mapa
    def mapa_de(cx, cy):
        return est["ox"] + cx / est["z"], est["oy"] + cy / est["z"]

    def tela_de(x, y):
        return (x - est["ox"]) * est["z"], (y - est["oy"]) * est["z"]

    def limitar():
        for eixo, tam, vis in (("ox", mapa.width, cw), ("oy", mapa.height, ch)):
            vista = vis / est["z"]
            est[eixo] = -(vista - tam) / 2 if vista >= tam else min(max(est[eixo], 0.0), tam - vista)

    def base_mapa():
        """Pedaço visível do mapa (com os baús), refeito só quando o zoom ou a posição mudam."""
        chave = (round(est["z"], 5), round(est["ox"], 2), round(est["oy"], 2), est["ver_baus"])
        if chave != est["base_chave"]:
            z = est["z"]
            img = Image.new("RGB", (cw, ch), AZUL_NOITE)
            x0, y0 = max(0.0, est["ox"]), max(0.0, est["oy"])
            x1, y1 = min(mapa.width, est["ox"] + cw / z), min(mapa.height, est["oy"] + ch / z)
            if x1 > x0 and y1 > y0:
                pedaco = mapa.resize((max(1, round((x1 - x0) * z)), max(1, round((y1 - y0) * z))),
                                     Image.BILINEAR, box=(x0, y0, x1, y1))
                img.paste(pedaco, (round((x0 - est["ox"]) * z), round((y0 - est["oy"]) * z)))
            if est["ver_baus"]:
                desenhar_baus(img, baus, tela_de, 2 if z < 0.5 else 3 if z < 1.5 else 5)
            est["base"], est["base_chave"] = img, chave
        return est["base"].copy()

    def alvo(cx, cy, livre=False):
        """Onde um clique em (cx, cy) põe o ponto: no baú a até 14 px de tela, a menos que esteja livre."""
        x, y = mapa_de(cx, cy)
        bau = None if livre or not est["grudar"] else bau_perto(baus, x, y, 14 / est["z"])
        x, y = bau or (x, y)
        return (min(max(x, 0), mapa.width - 1), min(max(y, 0), mapa.height - 1)), bau is not None

    def perto(cx, cy, raio=11):
        melhor = None
        for i, p in enumerate(pontos):
            d = math.hypot(*(a - b for a, b in zip(tela_de(*p), (cx, cy))))
            if d <= raio and (melhor is None or d < melhor[1]):
                melhor = (i, d)
        return None if melhor is None else melhor[0]

    def desenhar_mapa():
        limitar()
        img = base_mapa()
        z = est["z"]
        esp = 2 if z < 0.8 else 3 if z < 2 else 4
        tela_pts = [tela_de(*p) for p in pontos]
        d = ImageDraw.Draw(img)
        m = est["mouse"]
        if m and est["pega"] is None and est["pan"] is None and 0 <= m[0] < cw and 0 <= m[1] < ch \
                and perto(*m) is None:
            (ax, ay), no_bau = alvo(m[0], m[1], est["shift"])
            sx, sy = tela_de(ax, ay)
            seg = trecho_mais_perto(tela_pts, sx, sy) if est["ctrl"] else None
            de = [tela_pts[seg[0]], tela_pts[seg[0] + 1]] if seg else tela_pts[-1:]
            for px, py in de:  # prévia: de onde o próximo ponto sai
                d.line((px, py, sx, sy), fill=PRETO, width=4)
                d.line((px, py, sx, sy), fill=CIANO, width=2)
            r = 9 if no_bau else 5
            d.ellipse((sx - r - 2, sy - r - 2, sx + r + 2, sy + r + 2), outline=PRETO, width=4)
            d.ellipse((sx - r, sy - r, sx + r, sy + r), outline=CIANO, width=2)
        desenhar_rota_mini(img, tela_pts, esp)
        h = est["hover"] if est["pega"] is None else est["pega"]
        rotulos = set(rotulos_sem_empilhar(tela_pts, 16 + esp * 2)) | ({h} if h is not None else set())
        for i in sorted(rotulos):
            x, y = tela_pts[i]
            grande = i == h
            if grande:
                d.ellipse((x - 9, y - 9, x + 9, y + 9), outline=BRANCO, width=3)
            d.text((x + 6 + esp, y - 4 - esp), str(i + 1), font=fonte(18 if grande else 13 + esp),
                   fill=AMARELO if grande else BRANCO, stroke_width=2, stroke_fill=PRETO, anchor="ls")
        fotos["mapa"] = ImageTk.PhotoImage(img)
        tela.itemconfigure(item_mapa, image=fotos["mapa"])

    # ---------- cabeçalho e painel (mesmo visual da janela de configuração)
    def desenhar_topo():
        p = Pintor(W / f, TOPO_EDITOR, f)
        p.caixa(0, 0, W / f, TOPO_EDITOR, AZUL_FAIXA)
        p.caixa(0, 0, W / f, 4, CIANO)
        p.caixa(0, TOPO_EDITOR - 3, W / f, TOPO_EDITOR, AMARELO)
        p.circulo(34, 33, 22, alfa(AZUL_TOPO, 0.35))
        p.bau(34, 33, 2.6)
        p.texto_ouro(66, 42, "EDITOR DE ROTA", 30, 2.5, "ls")
        x = 66 + p.largura("EDITOR DE ROTA", 30) + 18
        p.texto(x, 34, nome.upper(), 18, CIANO, 1.2)
        if est["mudou"]:
            p.texto(W / f - 20, 34, "NÃO SALVO", 14, AMARELO, 1.2, "rm")
        fotos["topo"] = ImageTk.PhotoImage(p.final())
        tela.itemconfigure(item_topo, image=fotos["topo"])

    def tecla(p, id_, x0, y0, x1, y1, texto, cor=TECLA, cor_hover=TECLA_HOVER, aresta="#13307E", px=16,
              cor_texto=BRANCO):
        sobre = est["hover_painel"] == id_
        p.caixa(x0, y0 + 4, x1, y1 + 4, aresta, sk=8)
        p.caixa(x0, y0 + (1 if sobre else 0), x1, y1 + (1 if sobre else 0), cor_hover if sobre else cor, sk=8)
        p.caixa(x0 + 6, y0 + 1, x1 - 2, y0 + 3, alfa(BRANCO, 0.18), sk=1)
        p.texto((x0 + x1) / 2, (y0 + y1) / 2 + 0.5 + (1 if sobre else 0), texto, px, cor_texto, ancora="mm")
        regioes.append((x0, y0, x1, y1 + 4, id_))

    def amarela(p, id_, x0, y0, x1, y1, texto, px=16):
        tecla(p, id_, x0, y0, x1, y1, texto, AMARELO, "#FFE58A", "#B97C0E", px, PRETO)

    def etiqueta(p, y, texto):
        w = p.largura(texto, 15) + 30
        p.caixa(18, y, 18 + w, y + 24, AMARELO, sk=7, borda=PRETO, larg=1.2)
        p.texto(18 + 15, y + 12.5, texto, 15, PRETO)

    def desenhar_painel():
        regioes.clear()
        L = LADO_EDITOR
        p = Pintor(L, hs, f)
        p.degrade_em([(0, 0), (L, 0), (L, hs), (0, hs)], (misturar(AZUL_TOPO, AZUL_NOITE, 0.35), AZUL_NOITE))
        p.caixa(0, 0, 3, hs, AMARELO)
        etiqueta(p, 14, "ROTA")
        for n, (rotulo, valor) in enumerate((("PONTOS", len(pontos)), ("NOS BAÚS", pontos_nos_baus(pontos, baus)))):
            x = 18 + n * 121
            p.caixa(x, 48, x + 113, 102, CARTAO, sk=8, borda=BORDA, larg=1)
            p.texto(x + 14, 61, rotulo, 11, CINZA)
            p.texto(x + 14, 85, str(valor), 26, AMARELO, 1.5)
        amarela(p, ("salvar",), 18, 114, L - 18, 154, "SALVAR E FECHAR", 20)
        tecla(p, ("desfazer",), 18, 166, 131, 196, "DESFAZER")
        if est["limpar"]:
            tecla(p, ("limpar",), 139, 166, L - 18, 196, "CONFIRMAR?", VERMELHO, "#FF7A7A", "#8E1F1F")
        else:
            tecla(p, ("limpar",), 139, 166, L - 18, 196, "LIMPAR")
        etiqueta(p, 216, "MAPA")
        tecla(p, ("ver_rota",), 18, 250, 131, 280, "VER ROTA")
        tecla(p, ("ver_mapa",), 139, 250, L - 18, 280, "MAPA INTEIRO")
        for x0, x1, chave, texto in ((18, 131, "grudar", "GRUDAR NO BAÚ"), (139, L - 18, "ver_baus", "VER BAÚS")):
            if est[chave]:
                amarela(p, (chave,), x0, 292, x1, 322, texto, 14)
            else:
                tecla(p, (chave,), x0, 292, x1, 322, texto, px=14)
        etiqueta(p, 342, "COMO USAR")
        y = 378
        for chip, explica in EDITOR_AJUDA:
            if y > hs - 112:
                break  # tela baixa: a ajuda encolhe, os botões ficam
            w = p.largura(chip, 11) + 14
            p.caixa(18, y - 9, 18 + w, y + 9, TECLA_VAZIA, sk=4, borda=BORDA, larg=1)
            p.texto(18 + w / 2, y + 0.5, chip, 11, BRANCO, ancora="mm")
            p.texto(26 + w, y, explica, 12, CINZA, frase=True)
            y += 23
        cor = VERMELHO if est["erro"] else BRANCO
        p.circulo(24, hs - 96, 4, VERMELHO if est["erro"] else CIANO)
        linhas, atual = [], ""
        for palavra in est["status"].split():
            tentativa = f"{atual} {palavra}".strip()
            if p.largura(tentativa, 12, True) <= L - 54 or not atual:
                atual = tentativa
            else:
                linhas.append(atual)
                atual = palavra
        for i, linha in enumerate((linhas + [atual])[:3]):
            p.texto(36, hs - 96 + i * 16, linha, 12, cor, frase=True)
        tecla(p, ("sair",), 18, hs - 46, L - 18, hs - 16, "SAIR SEM SALVAR" if est["mudou"] else "FECHAR")
        fotos["painel"] = ImageTk.PhotoImage(p.final())
        tela.itemconfigure(item_painel, image=fotos["painel"])

    def avisar(texto, erro=False):
        est["status"], est["erro"] = texto, erro
        desenhar_painel()

    # ---------- ações
    def guardar():
        est["desfazer"].append(list(pontos))
        del est["desfazer"][:-200]
        if not est["mudou"]:
            est["mudou"] = True
            desenhar_topo()

    def desfazer(_e=None):
        if est["desfazer"]:
            pontos[:] = est["desfazer"].pop()
            avisar("Desfeito.")
            desenhar_mapa()
        else:
            avisar("Nada para desfazer.")

    def limpar():
        if not pontos:
            return avisar("A rota já está vazia.")
        if not est["limpar"]:
            est["limpar"] = True
            return avisar("Clique em CONFIRMAR para apagar todos os pontos. CTRL+Z desfaz.", erro=True)
        guardar()
        pontos.clear()
        est["limpar"] = False
        avisar("Rota apagada. CTRL+Z traz de volta.")
        desenhar_mapa()

    def ver(rota_inteira):
        est["z"], est["ox"], est["oy"] = vista_da_rota(pontos if rota_inteira else [], mapa.size, (cw, ch))
        desenhar_mapa()

    def salvar_e_sair(_e=None):
        if len(pontos) < 2:
            return avisar("A rota precisa de pelo menos 2 pontos para salvar.", erro=True)
        salvar_pontos(nome, pontos, rota["imagem"])
        salvar_rota(arquivo_cfg, rota, {"nome": nome, "ativo": True})
        est["salvo"] = True
        root.destroy()

    def sair(_e=None):
        if est["mudou"] and not est["sair"]:
            est["sair"] = True
            return avisar("Tem mudança não salva. ESC ou SAIR de novo fecha sem salvar.", erro=True)
        root.destroy()

    def acao_painel(id_):
        if id_[0] != "limpar":
            est["limpar"] = False
        if id_[0] != "sair":
            est["sair"] = False
        if id_[0] in ("grudar", "ver_baus"):
            est[id_[0]] = not est[id_[0]]
            avisar({"grudar": ("Os cliques grudam no baú mais perto.", "Os cliques ficam onde você clicar."),
                    "ver_baus": ("Baús do mapa visíveis.", "Baús do mapa escondidos.")}[id_[0]][0 if est[id_[0]] else 1])
            desenhar_mapa()
            return
        {"salvar": salvar_e_sair, "desfazer": desfazer, "limpar": limpar, "sair": sair,
         "ver_rota": lambda: ver(True), "ver_mapa": lambda: ver(False)}[id_[0]]()

    # ---------- mouse e teclado
    def no_painel(e):
        return e.x >= cw and e.y >= topo

    def regiao(e):
        x, y = (e.x - cw) / f, (e.y - topo) / f
        return next((r[4] for r in regioes if r[0] <= x <= r[2] and r[1] <= y <= r[3]), None)

    def no_mapa(e):
        return e.x < cw and e.y >= topo

    def mods(e):
        est["ctrl"], est["shift"] = bool(e.state & 0x4), bool(e.state & 0x1)

    def clique(e):
        mods(e)
        if no_painel(e):
            id_ = regiao(e)
            if id_:
                acao_painel(id_)
            return
        if not no_mapa(e):
            return
        cx, cy = e.x, e.y - topo
        est["limpar"] = est["sair"] = False
        i = perto(cx, cy)
        guardar()
        if i is None:
            (x, y), _ = alvo(cx, cy, est["shift"])
            seg = trecho_mais_perto([tela_de(*p) for p in pontos], cx, cy) if est["ctrl"] else None
            i = seg[0] + 1 if seg else len(pontos)
            pontos.insert(i, (x, y))
            avisar(f"Ponto {i + 1} " + ("inserido no meio da rota." if seg else "adicionado."))
        est["pega"] = i
        desenhar_mapa()

    def arrastar(e):
        mods(e)
        if est["pega"] is not None and no_mapa(e):
            pontos[est["pega"]] = alvo(e.x, e.y - topo, est["shift"])[0]
            est["mouse"] = (e.x, e.y - topo)
            desenhar_mapa()

    def soltar(_e):
        est["pega"] = None
        desenhar_mapa()

    def apagar(e):
        if not no_mapa(e):
            return
        i = perto(e.x, e.y - topo, 14)
        if i is not None:
            guardar()
            del pontos[i]
            est["hover"] = None
            avisar(f"Ponto {i + 1} apagado.")
            desenhar_mapa()

    def mover(e):
        mods(e)
        if no_painel(e):
            id_ = regiao(e)
            tela.configure(cursor="hand2" if id_ else "")
            if id_ != est["hover_painel"]:
                est["hover_painel"] = id_
                desenhar_painel()
            if est["mouse"] is not None:
                est["mouse"] = None
                desenhar_mapa()
            return
        if est["hover_painel"] is not None:
            est["hover_painel"] = None
            desenhar_painel()
        if no_mapa(e):
            est["mouse"] = (e.x, e.y - topo)
            est["hover"] = perto(*est["mouse"])
            tela.configure(cursor="fleur" if est["hover"] is not None else "crosshair")
            desenhar_mapa()

    def zoom(e):
        if not no_mapa(e):
            return
        cx, cy = e.x, e.y - topo
        mx, my = mapa_de(cx, cy)
        est["z"] = min(8.0, max(z_min, est["z"] * (1.15 if e.delta > 0 else 1 / 1.15)))
        est["ox"], est["oy"] = mx - cx / est["z"], my - cy / est["z"]  # o ponto sob o mouse fica parado
        desenhar_mapa()

    def pan_ini(e):
        est["pan"] = (e.x, e.y, est["ox"], est["oy"])

    def pan(e):
        if est["pan"]:
            x0, y0, ox, oy = est["pan"]
            est["ox"], est["oy"] = ox - (e.x - x0) / est["z"], oy - (e.y - y0) / est["z"]
            desenhar_mapa()

    def andar(dx, dy):
        est["ox"] += dx * 80 / est["z"]
        est["oy"] += dy * 80 / est["z"]
        desenhar_mapa()

    def tecla_mod(e, apertou):
        if e.keysym.startswith(("Control", "Shift")):
            est["ctrl" if e.keysym.startswith("Control") else "shift"] = apertou
            if est["mouse"]:
                desenhar_mapa()

    est["hover_painel"] = None
    item_topo = tela.create_image(0, 0, anchor="nw")
    item_mapa = tela.create_image(0, topo, anchor="nw")
    item_painel = tela.create_image(cw, topo, anchor="nw")
    tela.bind("<ButtonPress-1>", clique)
    tela.bind("<B1-Motion>", arrastar)
    tela.bind("<ButtonRelease-1>", soltar)
    tela.bind("<Button-3>", apagar)
    tela.bind("<ButtonPress-2>", pan_ini)
    tela.bind("<B2-Motion>", pan)
    tela.bind("<ButtonRelease-2>", lambda e: est.update(pan=None))
    tela.bind("<Motion>", mover)
    tela.bind("<Leave>", lambda e: (est.update(mouse=None, hover=None), desenhar_mapa()))
    tela.bind("<MouseWheel>", zoom)
    root.bind("<KeyPress>", lambda e: tecla_mod(e, True))
    root.bind("<KeyRelease>", lambda e: tecla_mod(e, False))
    root.bind("<Control-z>", desfazer)
    root.bind("<Control-s>", salvar_e_sair)
    root.bind("<Return>", salvar_e_sair)
    root.bind("<Escape>", sair)
    root.protocol("WM_DELETE_WINDOW", sair)
    for nome_tecla, (dx, dy) in {"Left": (-1, 0), "Right": (1, 0), "Up": (0, -1), "Down": (0, 1)}.items():
        root.bind(f"<{nome_tecla}>", lambda e, dx=dx, dy=dy: andar(dx, dy))
    if not baus:
        est["status"], est["erro"] = "Sem baús: baixe o mapa e os baús na página ROTA NO MAPA.", True
    desenhar_topo()
    desenhar_painel()
    desenhar_mapa()
    root.focus_force()
    root.mainloop()
    if est["salvo"]:
        print(f"Rota {nome!r} salva com {len(pontos)} pontos em {PASTA_ROTAS / (nome + '.json')} e ativada.")
    else:
        print("Saiu sem salvar.")


def janela_ajuste(titulo, fazer_imagem, x, y, largura, alfa, minimo=60):
    """Janela arrastável por cima do jogo: scroll muda o tamanho, setas dão ajuste fino, Enter salva.

    `fazer_imagem(largura)` devolve a imagem PIL nesse tamanho. Devolve (x, y, largura) ou None se cancelar.
    """
    import tkinter as tk
    from PIL import ImageTk
    root = tk.Tk()
    root.title(titulo)
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", alfa)
    primeira = fazer_imagem(largura)
    est = {"largura": largura, "salvo": False,
           "x": x if x is not None else (root.winfo_screenwidth() - primeira.width) // 2,
           "y": y if y is not None else (root.winfo_screenheight() - primeira.height) // 2}
    rotulo = tk.Label(root, bd=0, cursor="fleur")
    rotulo.pack()

    def desenhar():
        img = fazer_imagem(est["largura"]).convert("RGB")
        est["altura"] = img.height
        ImageDraw.Draw(img).rectangle((0, 0, img.width - 1, img.height - 1), outline=AMARELO, width=2)
        est["foto"] = ImageTk.PhotoImage(img)
        rotulo.configure(image=est["foto"])
        root.geometry(f"{img.width}x{img.height}+{est['x']}+{est['y']}")

    def pegar(e):
        est["dx"], est["dy"] = e.x_root - est["x"], e.y_root - est["y"]

    def arrastar(e):
        est["x"], est["y"] = e.x_root - est["dx"], e.y_root - est["dy"]
        root.geometry(f"+{est['x']}+{est['y']}")

    def escala(e):
        nova = round(est["largura"] * (1.03 if e.delta > 0 else 1 / 1.03))
        if nova == est["largura"]:
            nova += 1 if e.delta > 0 else -1
        nova = min(10000, max(minimo, nova))
        # mantém no lugar o ponto que está embaixo do mouse
        rx, ry = (e.x_root - est["x"]) / est["largura"], (e.y_root - est["y"]) / est["altura"]
        proporcao = est["altura"] / est["largura"]
        est["x"] -= round((nova - est["largura"]) * rx)
        est["y"] -= round((nova - est["largura"]) * proporcao * ry)
        est["largura"] = nova
        desenhar()

    def mover(dx, dy):
        est["x"] += dx
        est["y"] += dy
        root.geometry(f"+{est['x']}+{est['y']}")

    def salvar_ajuste(_e=None):
        est["salvo"] = True
        root.destroy()

    rotulo.bind("<ButtonPress-1>", pegar)
    rotulo.bind("<B1-Motion>", arrastar)
    rotulo.bind("<Double-Button-1>", salvar_ajuste)
    rotulo.bind("<Button-3>", lambda e: root.destroy())
    root.bind("<MouseWheel>", escala)
    root.bind("<Return>", salvar_ajuste)
    root.bind("<Escape>", lambda e: root.destroy())
    for tecla, (dx, dy) in {"Left": (-1, 0), "Right": (1, 0), "Up": (0, -1), "Down": (0, 1)}.items():
        root.bind(f"<{tecla}>", lambda e, dx=dx, dy=dy: mover(dx, dy))
    desenhar()
    root.focus_force()
    root.mainloop()
    return (est["x"], est["y"], est["largura"]) if est["salvo"] else None


def salvar_rota(arquivo_cfg, rota, mudancas, minimapa=None):
    """Grava no config.json só o bloco "rota" (com as mudanças), sem mexer no resto do arquivo."""
    try:
        dados = json.loads(arquivo_cfg.read_text(encoding="utf-8")) if arquivo_cfg.exists() else {}
    except (OSError, ValueError):
        dados = {}
    if not isinstance(dados, dict):
        dados = {}
    bloco = dict(copia(rota), **(dados.get("rota") if isinstance(dados.get("rota"), dict) else {}))
    bloco.update(mudancas)
    if minimapa is not None:
        atual = bloco.get("minimapa") if isinstance(bloco.get("minimapa"), dict) else {}
        bloco["minimapa"] = {**copia(rota["minimapa"]), **atual, **minimapa}
    dados["rota"] = ler_rota(bloco)  # valida e limpa chaves de versões antigas (ex.: as do mapa grande)
    arquivo_cfg.write_text(json.dumps(dados, indent=2, ensure_ascii=False), encoding="utf-8")


def marcar_minimapa(cfg, arquivo_cfg):
    """Marca onde o minimapa fica na tela: um quadrado para pôr exatamente em cima dele."""
    rota = cfg["rota"]
    m = rota["minimapa"]

    def fazer(lado):
        img = Image.new("RGB", (lado, lado), AZUL_NOITE)
        d = ImageDraw.Draw(img)
        c = lado // 2
        d.line((c, 0, c, lado), fill=AMARELO, width=1)
        d.line((0, c, lado, c), fill=AMARELO, width=1)
        y = 6
        for linha in ("QUADRADO = MINIMAPA", "a cruz fica na seta", "do personagem", "ENTER salva", "ESC cancela"):
            d.text((8, y), linha, font=fonte(12), fill=BRANCO, stroke_width=2, stroke_fill=PRETO)
            y += 16
        return img

    r = janela_ajuste("Marcar minimapa", fazer, m["x"], m["y"], m["lado"] or 255, 0.4)
    if r is None:
        print("Cancelado, nada foi salvo.")
        return
    novo = {"x": r[0], "y": r[1], "lado": r[2]}
    if m["escala"] and m["lado"] and r[2] != m["lado"]:  # o minimapa mostra a mesma área: escala × lado fica igual
        novo["escala"] = round(m["escala"] * m["lado"] / r[2], 2)
    salvar_rota(arquivo_cfg, rota, {"ativo": True}, novo)
    print(f"Minimapa salvo: x={r[0]} y={r[1]} lado={r[2]}")

# Ferramentas de rota pela janela de configuração (o .exe não tem console nem os .bat)
VERSAO_MAPA = "43.10"  # versão conhecida do mapa; o botão BAIXAR tenta antes a mais nova do site
MODELO_MAPA = "https://chestroute.com/tiles/{versao}/real/{z}/{x}/{y}.webp"
SITE_BAUS = "https://chestroute.com"


def rota_do_arquivo(arquivo_cfg):
    """Bloco "rota" como está gravado agora no config.json (as ferramentas gravam direto lá)."""
    try:
        dados = json.loads(arquivo_cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        dados = {}
    return ler_rota(dados.get("rota") if isinstance(dados, dict) else None)


def minimapa_disponivel():
    """True se dá para seguir o personagem no minimapa (OpenCV, numpy e mss instalados)."""
    import importlib.util
    try:
        return all(importlib.util.find_spec(m) is not None for m in ("cv2", "numpy", "mss", "seguir_mapa"))
    except (ImportError, ValueError):
        return False


def estado_rota(rota, pasta=None):
    """O que já está pronto, para a página de rota: mapa baixado, baús, rotas salvas e minimapa possível."""
    pasta = pasta or PASTA
    return {"mapa": (pasta / rota["imagem"]).is_file(), "baus": len(carregar_baus(rota, pasta)),
            "rotas": listar_rotas(pasta / "rotas"), "mini": minimapa_disponivel()}


def atalhos_repetidos(cfg):
    """Teclas da rota que também são atalhos do contador (as duas ações disparariam juntas)."""
    rota = {cfg["rota"]["trocar"]} - {""}
    return sorted(rota & {n for nomes in cfg["atalhos"].values() for n in nomes if n})


def nome_nova_rota(nomes):
    """Primeiro "rotaN" livre (sem diferenciar maiúsculas: no Windows Rota1.json e rota1.json são o mesmo)."""
    usados, n = {s.lower() for s in nomes}, 1
    while f"rota{n}" in usados:
        n += 1
    return f"rota{n}"


def proxima_rota(nomes, atual, passo):
    """Rota seguinte (passo 1) ou anterior (-1) na lista; "" se não há rotas."""
    if not nomes:
        return ""
    i = nomes.index(atual) if atual in nomes else (-1 if passo > 0 else 0)
    return nomes[(i + passo) % len(nomes)]


def rodar_ferramenta(funcao, *args):
    """Roda uma ferramenta de rota (que fala por print e sys.exit) e devolve (linhas, falhou).

    No .exe não há console: sem isso as mensagens sumiriam. Tudo vai também para o log.
    """
    buf, falhou = io.StringIO(), False
    try:
        with contextlib.redirect_stdout(buf):
            funcao(*args)
    except SystemExit as e:
        falhou = True
        if e.code not in (None, 0):
            buf.write(f"\n{e.code}")
    except Exception:
        falhou = True
        log.exception("Erro em %s", funcao.__name__)
        buf.write("\nErro inesperado: veja o chesttracker.log")
    linhas = [s.strip() for s in buf.getvalue().splitlines() if s.strip()]
    for s in linhas:
        log.info("%s: %s", funcao.__name__, s)
    return linhas, falhou


def cancelou(linhas):
    return any("cancel" in s.lower() or "sem salvar" in s.lower() for s in linhas)


def baixar_mapa_e_baus(cfg, arquivo_cfg, ler_versao=None, comando=None):
    """Botão BAIXAR: mapa e baús da versão mais nova do chestroute.com, sem console. Devolve (mensagem, erro).

    Se a versão nova ainda não tem ladrilhos, tenta a VERSAO_MAPA conhecida.
    """
    ler_versao = ler_versao or (lambda: ler_baus_site(SITE_BAUS)[0])
    comando = comando or comando_baixar_mapa
    try:
        versoes = [ler_versao()]
    except (OSError, ValueError) as e:
        log.warning("Não deu para descobrir a versão do mapa no site (%s); usando %s", e, VERSAO_MAPA)
        versoes = []
    if VERSAO_MAPA not in versoes:
        versoes.append(VERSAO_MAPA)
    for versao in versoes:
        linhas, falhou = rodar_ferramenta(comando, cfg, arquivo_cfg, MODELO_MAPA.replace("{versao}", versao),
                                          4, 1.8, SITE_BAUS)
        if not falhou:
            break
    if falhou:
        return (linhas[-1] if linhas else "Não deu para baixar o mapa."), True
    aviso = next((s for s in linhas if s.startswith("AVISO")), None)
    if aviso:
        return f"Mapa {versao} baixado, mas os baús não vieram: {aviso.split(':', 2)[-1].strip()}", True
    n = len(carregar_baus(rota_do_arquivo(arquivo_cfg)))
    return f"Mapa {versao} baixado com {n} baús. Agora desenhe a sua rota (passo 2).", False


def rodar_ferramenta_rota(acao, cfg, arquivo_cfg, nome=""):
    """EDITAR e MARCAR da página de rota: abrem a janela da ferramenta. Devolve (mensagem, erro)."""
    funcao, args = {"editar": (editar_rota, (cfg, arquivo_cfg, nome)), "marcar": (marcar_minimapa, (cfg, arquivo_cfg))}[acao]
    linhas, falhou = rodar_ferramenta(funcao, *args)
    if falhou:
        return (linhas[-1] if linhas else "A ferramenta fechou com erro: veja o chesttracker.log"), True
    if cancelou(linhas):
        return "Fechado sem salvar.", False
    return {"editar": f"Rota {nome!r} salva e ativada. Agora marque o minimapa (passo 3).",
            "marcar": "Minimapa marcado. Numa partida, a rota acompanha o personagem."}[acao], False


def novo_anim(cont, token, t):
    """Estado das animações do painel."""
    return {"visto": cont.total, "pop": float("-inf"), "inicio": t, "fase": "?", "fase_t": float("-inf"),
            "musica": None, "musica_t": float("-inf"), "barra": chance_token(cont.total, token), "chave": None}


def overlay(cont, cfg, estado, fase, arquivo_cfg=None, medidor=None):
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
    W, H = round(PW * k), round(ALTURA * k)
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
    repetidos = atalhos_repetidos(cfg)
    if cfg["rota"]["ativo"] and repetidos:
        log.warning("Rota: %s também é atalho do contador; troque no bloco \"rota\" do config.json",
                    ", ".join(exibir(n) for n in repetidos))
    fonte_rota = abrir_fonte(cfg["rota"])
    iniciar_teclas_rota(cfg["rota"], fonte_rota)
    # só lê o minimapa em partida (ou sem log do jogo) e com o Fortnite em primeiro plano
    minirota = iniciar_minirota(root, cfg["rota"], fonte_rota,
                                lambda: fase.atual in ("partida", None) and jogo_na_frente(),
                                (lambda e: salvar_rota(arquivo_cfg, cfg["rota"], {}, {"escala": e, "confirmada": True}))
                                if arquivo_cfg else None)
    if medidor is not None and minirota is not None:
        minirota.rastreador.medir = lambda tipo, ms: medidor.anotar(f"minimapa: {tipo}", ms)

    def tick():
        if estado["sair"]:
            root.destroy()
            return
        t = time.monotonic()
        if medidor is not None and medidor.tick(t, fase.atual):
            texto = medidor.relatorio()
            try:
                (PASTA / "desempenho.txt").write_text(texto, encoding="utf-8")
            except OSError as e:
                log.error("Não deu para gravar desempenho.txt (%s)", e)
            log.info("Teste de desempenho:\n%s", texto)
            root.destroy()
            return
        try:
            t0 = time.perf_counter()
            img = quadro(cont, t, k, tecla, anim, atalhos, cfg["token"], fase, estado.get("musica"))
            if img is not None:
                if medidor is not None:
                    medidor.anotar("painel (desenhar)", (time.perf_counter() - t0) * 1000)
                foto.paste(img)
        except Exception:  # um quadro com erro não pode parar o painel
            log.exception("Erro ao desenhar o painel")
        try:
            if fonte_rota is not None and fonte_rota.aplicar():
                log.info("Rota trocada para %r", fonte_rota.nome)
            if minirota is not None:
                t0 = time.perf_counter()
                minirota.atualizar(t)
                if medidor is not None:
                    medidor.anotar("minimapa (desenhar)", (time.perf_counter() - t0) * 1000)
        except Exception:  # rota com erro não pode parar o painel
            log.exception("Erro ao desenhar a rota")
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
    # rota: só linha amarela e seta branca sobram; teclas ligam/desligam
    mapa = Image.new("RGB", (60, 40), (200, 120, 80))
    dm = ImageDraw.Draw(mapa)
    dm.line((5, 20, 55, 20), fill=(255, 220, 30), width=5)
    dm.rectangle((25, 5, 33, 13), fill=(255, 255, 255))
    so = extrair_rota(mapa)
    chave_rgb = ImageColor.getrgb(CHAVE)
    assert so.getpixel((30, 20)) == (255, 220, 30) and so.getpixel((29, 9)) == (255, 255, 255)
    assert so.getpixel((2, 2)) == chave_rgb and so.getpixel((30, 35)) == chave_rgb
    assert so.getpixel((30, 16)) == (0, 0, 0)                           # contorno preto em volta da linha
    rt = ler_rota({"ativo": True, "trocar": " F6 ", "espessura": 5, "mostrar_baus": "x", "tecla": "m"})
    assert rt["ativo"] and rt["trocar"] == "f6" and rt["espessura"] == 2 and rt["mostrar_baus"] is False
    assert "tecla" not in rt and ler_rota({"espessura": 3})["espessura"] == 3
    assert ler_rota({"espessura": True})["espessura"] == 2 and ler_rota(None) == PADRAO["rota"]
    rm = ler_rota({"minimapa": {"ativo": True, "x": 1650, "y": 20.4, "lado": 255, "escala": 2.3, "hz": 99}})
    assert rm["minimapa"] == {"x": 1650, "y": 20, "lado": 255, "escala": 2.3, "hz": 4, "confirmada": False}
    assert ler_rota({"minimapa": {"confirmada": True}})["minimapa"]["confirmada"] is True
    grossa = engrossar_rota(so)
    assert grossa.getpixel((30, 20)) == (255, 220, 30) and grossa.getpixel((30, 24)) == (255, 220, 30)
    assert grossa.getpixel((30, 26)) == (0, 0, 0) and grossa.getpixel((2, 38)) == chave_rgb
    rec = recorte_minimapa(so, 30, 20, 0.2, 100)                       # 20 px da rota viram 100 px
    assert rec.size == (100, 100) and rec.getpixel((50, 50)) == (255, 220, 30)
    borda = recorte_minimapa(so, 0, 0, 0.4, 100)                       # metade do recorte cai fora da imagem
    assert borda.getpixel((5, 5)) == chave_rgb and borda.getpixel((95, 60)) == chave_rgb and borda.getpixel((95, 98)) == (255, 220, 30)
    arq = Path(tempfile.mkdtemp()) / "config.json"
    arq.write_text(json.dumps({"min": 1.0, "rota": {"tecla": "tab"}}), encoding="utf-8")
    salvar_rota(arq, PADRAO["rota"], {"ativo": True}, {"x": 5, "y": 6, "lado": 200})
    salvo = json.loads(arq.read_text(encoding="utf-8"))
    assert salvo["min"] == 1.0 and "tecla" not in salvo["rota"] and salvo["rota"]["ativo"] is True
    assert salvo["rota"]["minimapa"] == {"x": 5, "y": 6, "lado": 200, "escala": 0.0, "hz": 4, "confirmada": False}
    assert ler_rota(salvo["rota"])["minimapa"]["lado"] == 200

    # rotas desenhadas no editor: arquivo, desenho, troca
    assert nome_valido("rota 1_b-c") and not nome_valido("../x") and not nome_valido("") and not nome_valido(" a")
    assert ler_rota({"nome": "minha rota", "trocar": "F6"})["nome"] == "minha rota"
    assert ler_rota({"nome": "a/b"})["nome"] == "" and ler_rota({"trocar": "F5"})["trocar"] == "f5"
    pr = Path(tempfile.mkdtemp())
    assert listar_rotas(pr / "nada") == []
    salvar_pontos("b", [(10.04, 20), (50, 20)], "rota.png", pr)
    salvar_pontos("a", [(30, 30)], "rota.png", pr)
    assert listar_rotas(pr) == ["a", "b"] and carregar_pontos("b", pr) == [(10.0, 20.0), (50.0, 20.0)]
    (pr / "ruim.json").write_text('{"pontos": [[1, "x"]]}', encoding="utf-8")
    try:
        carregar_pontos("ruim", pr)
        raise AssertionError("rota inválida devia falhar")
    except ValueError:
        pass
    tela_p = desenhar_pontos(Image.new("RGB", (120, 60), CHAVE), [(10, 30), (110, 30)], 4, numeros=True)
    assert tela_p.getpixel((10, 30)) == ImageColor.getrgb(VERDE)        # começo em verde
    assert tela_p.getpixel((110, 30)) == ImageColor.getrgb(AMARELO)
    assert tela_p.getpixel((35, 30)) == ImageColor.getrgb(AMARELO)      # linha
    assert tela_p.getpixel((60, 30)) == (255, 255, 255)                 # seta no meio do trecho
    assert tela_p.getpixel((60, 55)) == chave_rgb
    assert desenhar_pontos(Image.new("RGB", (9, 9), CHAVE), []).getpixel((4, 4)) == chave_rgb
    (pr / "ruim.json").unlink()
    fr = FonteRota(dict(PADRAO["rota"], nome="b"), mapa=Image.new("RGB", (200, 100), (90, 140, 90)), pasta=pr)
    assert fr.pontos == [(10.0, 20.0), (50.0, 20.0)]
    mini = fr.mini(30, 20, 0.5, 100)                                    # jogador no meio do trecho; 1 px = 0,5
    assert mini.size == (100, 100) and mini.getpixel((50, 50)) == (255, 255, 255)   # seta do trecho no centro
    assert mini.getpixel((10, 50)) == ImageColor.getrgb(VERDE) and mini.getpixel((50, 90)) == chave_rgb
    assert not fr.aplicar() and fr.versao == 0
    fr.pedir_troca()
    assert fr.aplicar() and fr.nome == "a" and fr.versao == 1 and fr.pontos == [(30.0, 30.0)]
    fr.pedir_troca(); fr.aplicar(); assert fr.nome == "b"
    legado = FonteRota(dict(PADRAO["rota"]), mapa=mapa, pasta=pr)       # sem nome: linhas da própria imagem
    assert legado.pontos is None
    assert legado.mini(30, 20, 0.2, 100).getpixel((50, 50)) == (255, 220, 30)
    faltando = FonteRota(dict(PADRAO["rota"], nome="nao_existe"), mapa=mapa, pasta=pr)
    assert faltando.pontos == []
    # baixar mapa em ladrilhos: junta, corta na área que existe, 404 vira fundo
    def ladrilho(cor):
        b = io.BytesIO()
        Image.new("RGB", (16, 16), cor).save(b, "PNG")
        return b.getvalue()

    pedidos = []
    grade = {(1, 1): (200, 0, 0), (2, 1): (0, 200, 0), (2, 2): (0, 0, 200)}   # zoom 2 = 4x4; só 3 existem

    def servidor(url):
        pedidos.append(url)
        x, y = (int(v) for v in re.search(r"/2/(\d+)/(\d+)\.png$", url).groups())
        return ladrilho(grade[(x, y)]) if (x, y) in grade else None

    dm_arq = Path(tempfile.mkdtemp()) / "mapa.png"
    assert baixar_mapa("https://s.test/t/{z}/{x}/{y}.png", 2, dm_arq, servidor, 2, lambda m: None) \
        == (32, 32, 3, (16, 16))
    assert len(pedidos) == 16 and "https://s.test/t/2/3/0.png" in pedidos
    junto = Image.open(dm_arq)
    assert junto.getpixel((8, 8)) == (200, 0, 0) and junto.getpixel((24, 8)) == (0, 200, 0)
    assert junto.getpixel((24, 24)) == (0, 0, 200) and junto.getpixel((8, 24)) == ImageColor.getrgb(AZUL_NOITE)
    for ruim_modelo, ruim_zoom in (("https://s.test/{z}/{x}.png", 2), ("s.test/{z}/{x}/{y}", 2),
                                   ("https://s.test/{z}/{x}/{y}", 9)):
        try:
            baixar_mapa(ruim_modelo, ruim_zoom, dm_arq, servidor)
            raise AssertionError("modelo/zoom inválido devia falhar")
        except ValueError:
            pass
    try:
        baixar_mapa("https://s.test/{z}/{x}/{y}.png", 1, dm_arq, lambda url: None, 2, lambda m: None)
        raise AssertionError("sem ladrilhos devia falhar")
    except OSError:
        pass
    # baús de um site no formato do chestroute: acha o arquivo da versão certa, converte e desenha
    site_falso = {
        "https://b.test/": b'<script src="/assets/index-Ab_1.js"></script>',
        "https://b.test/assets/index-Ab_1.js": (
            b'x({"../../../data/43.00/chests.json":()=>Oe(()=>import("./chests-velho.js"),[]).then(c=>c.default),'
            b'"../../../data/43.10/chests.json":()=>Oe(()=>import("./chests-novo.js"),[]).then(c=>c.default)})'),
        "https://b.test/assets/chests-novo.js": (
            b"const a=JSON.parse('[{\"id\":\"n-0\",\"x\":100,\"y\":50.5,\"type\":\"normal\",\"prob\":1},"
            b"{\"id\":\"b-1\",\"x\":120,\"y\":60,\"type\":\"buried\",\"prob\":0.67},{\"id\":\"ruim\"}]');"
            b"export{a as default};"),
        "https://b.test/assets/chests-velho.js": b"const a=JSON.parse('[{\"x\":1,\"y\":1}]');export{a as default};",
    }
    vb, lb = ler_baus_site("https://b.test/", None, site_falso.get)
    assert vb == "43.10" and [b["x"] for b in lb] == [100, 120]
    assert ler_baus_site("https://b.test", "43.00", site_falso.get)[1] == [{"x": 1, "y": 1}]
    try:
        ler_baus_site("https://vazio.test", None, {"https://vazio.test/": b"<html></html>"}.get)
        raise AssertionError("site sem script devia falhar")
    except ValueError:
        pass
    pb = Path(tempfile.mkdtemp())
    assert salvar_baus(lb, 4, (150, 80), "mapa.jpg", pb / ARQ_BAUS, "https://b.test", vb) == 2
    bs = carregar_baus({"imagem": "mapa.jpg"}, pb)                       # zoom 4 = dobro do zoom 3, menos a origem
    assert bs == [(50.0, 21.0, "normal", 1.0), (90.0, 40.0, "buried", 0.67)]
    assert carregar_baus({"imagem": "outro.png"}, pb) == [] and carregar_baus({"imagem": "mapa.jpg"}, pb / "x") == []
    assert bau_perto(bs, 53, 23, 10) == (50.0, 21.0) and bau_perto(bs, 70, 30, 10) is None
    quadro_b = desenhar_baus(Image.new("RGB", (120, 60), CHAVE), bs, lambda x, y: (x, y), 4)
    assert quadro_b.getpixel((50, 21)) == ImageColor.getrgb(COR_BAU["normal"])
    assert quadro_b.getpixel((90, 40)) == (0, 0, 0)                      # incerto: só o contorno colorido
    assert quadro_b.getpixel((87, 40)) == ImageColor.getrgb(COR_BAU["buried"]) and quadro_b.getpixel((5, 5)) == chave_rgb
    pc = Path(tempfile.mkdtemp())
    salvar_pontos("velha", [(10, 10), (60, 30)], "rota.png", pc)
    salvar_pontos("nova", [(1, 1)], "mapa.jpg", pc)
    assert converter_rotas("rota.png", "mapa.jpg", 0.5, 46, 15, bs, pc) == ["velha"]
    assert carregar_pontos("velha", pc) == [(50.0, 21.0), (76.0, 30.0)]  # 1º grudou no baú; 2º ficou livre
    assert carregar_pontos("velha", pc / "antigas") == [(10.0, 10.0), (60.0, 30.0)]
    assert json.loads((pc / "velha.json").read_text(encoding="utf-8"))["mapa"] == "mapa.jpg"
    assert converter_rotas("rota.png", "mapa.jpg", 0.5, 46, 15, bs, pc) == []       # já convertida: não repete
    com_baus = FonteRota(dict(PADRAO["rota"], nome="b", mostrar_baus=True, imagem="mapa.jpg"),
                         mapa=Image.new("RGB", (200, 100), (90, 140, 90)), pasta=pr)
    com_baus.baus = bs
    assert com_baus.mini(90, 40, 0.5, 100).getpixel((49, 50)) == ImageColor.getrgb(COR_BAU["buried"])
    assert ler_rota({"mostrar_baus": True})["mostrar_baus"] is True and not PADRAO["rota"]["mostrar_baus"]
    tr = TeclasRota("f6", fr)
    tr.descer("f6", 0); assert fr.aplicar() and fr.nome == "a"
    tr.descer("m", 0); tr.subir("f6", 0); assert not fr.aplicar()
    # minimapa: linha fina, bolinha por baú sem empilhar, seta só em trecho comprido
    rm_img = desenhar_rota_mini(Image.new("RGB", (80, 40), CHAVE), [(20, 20), (21, 20), (22, 21), (60, 20)])
    assert rm_img.getpixel((20, 20)) == ImageColor.getrgb(VERDE) and rm_img.getpixel((60, 20)) == ImageColor.getrgb(AMARELO)
    assert rm_img.getpixel((41, 20)) == (255, 255, 255) and rm_img.getpixel((40, 30)) == chave_rgb
    juntos = desenhar_rota_mini(Image.new("RGB", (80, 40), CHAVE), [(20, 20), (24, 20)], 1)
    assert juntos.getpixel((24, 17)) == chave_rgb                        # 2º baú colado: sem bolinha empilhada
    # na fila só o pulso anima: no máximo 10 quadros por segundo (antes eram 30)
    cf, ff = Contador(0.85, 3.0, 5), Fase()
    ff.base, ff.fila = "lobby", True
    af = novo_anim(cf, 137500, 0)
    for i in range(30):  # 1º segundo: a troca de fase anima a 30 quadros por segundo, como deve
        quadro(cf, 50 + i / 30, 1.0, "E", af, "", 137500, ff)
    assert sum(quadro(cf, 51 + i / 30, 1.0, "E", af, "", 137500, ff) is not None for i in range(90)) <= 32
    # modo --medir: CPU em % do PC, pior trecho, partes e fases
    rel_t, rel_c = [0.0], [0.0]
    md = Medidor(30, relogio=lambda: rel_t[0], cpu=lambda: rel_c[0], nucleos=4)
    for i in range(1, 7):  # 30 s: 5 s com 1% de um núcleo, um trecho de 5 s com 40%
        rel_t[0] += 5; rel_c[0] += 0.05 if i != 3 else 2.0
        acabou = md.tick(rel_t[0], "partida" if i > 2 else "lobby")
    assert acabou and md.amostras[-1] == (30.0, rel_c[0])
    md.anotar("minimapa: perto", 8.0); md.anotar("minimapa: perto", 12.0)
    rel_md = md.relatorio()
    assert "VEREDITO:" in rel_md and "10.0 ms" in rel_md and "12.0 ms" in rel_md
    assert "(40% de um núcleo)" in rel_md and "em partida" in rel_md and "4 núcleos" in rel_md
    assert memoria_mb()[0] >= 0
    # editor de rota: vista inicial, trecho para inserir, números sem empilhar
    assert vista_da_rota([], (100, 200), (50, 50)) == (0.25, -50.0, 0.0)  # sem rota: mapa inteiro, centrado
    assert vista_da_rota([(10, 10), (30, 30)], (1000, 1000), (100, 100), margem=10) == (2.5, 0.0, 0.0)
    assert trecho_mais_perto([(0, 0), (10, 0), (10, 10)], 5, 1) == (0, 1.0)
    assert trecho_mais_perto([(0, 0), (10, 0), (10, 10)], 11, 5) == (1, 1.0) and trecho_mais_perto([(1, 1)], 0, 0) is None
    assert rotulos_sem_empilhar([(0, 0), (5, 0), (30, 0), (31, 0)], 18) == [0, 3]
    assert rotulos_sem_empilhar([(0, 0), (40, 0)], 18) == [0, 1] and rotulos_sem_empilhar([]) == []
    assert pontos_nos_baus([(50.0, 21.0), (1, 1), (90.3, 40)], bs) == 2
    # rodinha do mouse: giros seguidos viram uma rajada, tratada como tecla segurada
    assert exibir("roda_cima") == "RODA ↑" and exibir("roda_baixo", True) == "R↓"
    assert nome_rolagem(1) == "roda_cima" and nome_rolagem(-120) == "roda_baixo" and nome_rolagem(0) is None
    cr = Contador(0.85, 3.0, 0)
    mudancas = []
    er = Entrada(cr, {"segurar": ["e", "roda_baixo"], "somar": ["roda_cima", ""], "descontar": ["", ""],
                      "zerar": ["", ""], "sair": ["", ""]}, lambda: mudancas.append(cr.total), lambda: None)
    raj = RajadaRoda(er, {"roda_baixo", "roda_cima"})
    for t in (10.0, 10.2, 10.5, 10.95):          # vasculhou girando por 0,95 s, sem pausa de 0,4 s
        raj.giro("roda_baixo", t)
    raj.checar(11.2)
    assert cr.total == 0 and "roda_baixo" in raj.ultimo   # pausa de 0,25 s: ainda girando
    raj.checar(11.4)
    assert cr.total == 1 and abs(cr.ultima - 0.95) < 1e-9 and not raj.ultimo  # soltou no último giro: BAÚ +1
    for t in (20.0, 20.3):                        # rajada curta: não vasculhou
        raj.giro("roda_baixo", t)
    raj.checar(21.0)
    assert cr.total == 1
    for t in (30.0, 30.1, 30.2):                  # rodinha para cima = +1, uma vez por rajada
        raj.giro("roda_cima", t)
    raj.checar(31.0)
    assert cr.total == 2 and mudancas == [1, 2]
    RajadaRoda(er, {"roda_cima"}).giro("roda_baixo", 40.0)   # giro que não é atalho: ignorado
    assert cr.segurando(40.1) is None
    # lote de Raw Input (64 bits): mexeu o mouse, rodinha ↑, teclado, rodinha ↓
    def bloco_mouse(flags, delta):
        return struct.pack("<IIQQ", 0, 48, 0, 0) + struct.pack("<HHHhIiiI", 0, 0, flags, delta, 0, 5, -3, 0)
    lote = bloco_mouse(0, 0) + bloco_mouse(0x0400, 120) + struct.pack("<IIQQ", 1, 40, 0, 0) + bytes(16) + \
        bloco_mouse(0x0400 | 0x0001, -240)
    assert giros_do_lote(lote, 4, 24) == ["roda_cima", "roda_baixo"]
    assert giros_do_lote(bloco_mouse(0x0800, 120), 1, 24) == []   # rodinha lateral não conta
    # Spotify: desligado por padrão; o config.json liga
    assert PADRAO["spotify"] is False
    arq_sp = Path(tempfile.mkdtemp()) / "config.json"
    arq_sp.write_text('{"spotify": true}', encoding="utf-8")
    assert carregar_config(arq_sp)["spotify"] is True
    arq_sp.write_text('{"spotify": "sim"}', encoding="utf-8")
    assert carregar_config(arq_sp)["spotify"] is False
    # página ROTA NO MAPA: ferramentas sem console, download com versão nova e reserva
    assert nome_nova_rota(["Rota1", "rota2", "outra"]) == "rota3" and nome_nova_rota([]) == "rota1"
    assert proxima_rota(["a", "b", "c"], "c", 1) == "a" and proxima_rota(["a", "b"], "a", -1) == "b"
    assert proxima_rota(["a", "b"], "x", 1) == "a" and proxima_rota(["a", "b"], "x", -1) == "b"
    assert proxima_rota([], "a", 1) == ""
    assert atalhos_repetidos(PADRAO) == []
    bate = copia(PADRAO); bate["atalhos"]["zerar"] = ["m", "f6"]
    assert atalhos_repetidos(bate) == ["f6"]
    pe = Path(tempfile.mkdtemp())
    assert rota_do_arquivo(pe / "nao_existe.json") == PADRAO["rota"]
    info = estado_rota(PADRAO["rota"], pe)
    assert info["mapa"] is False and info["baus"] == 0 and info["rotas"] == []
    Image.new("RGB", (8, 8)).save(pe / "mapa.jpg")
    salvar_pontos("r1", [(1, 1)], "mapa.jpg", pe / "rotas")
    info = estado_rota(PADRAO["rota"], pe)
    assert info["mapa"] and info["rotas"] == ["r1"]

    def fala(*a):
        print("linha 1")
        print("AVISO: algo")

    def morre(*a):
        print("tentando")
        sys.exit("Não deu: sem rede")

    def quebra(*a):
        raise RuntimeError("x")

    assert rodar_ferramenta(fala) == (["linha 1", "AVISO: algo"], False)
    assert rodar_ferramenta(morre) == (["tentando", "Não deu: sem rede"], True)
    assert rodar_ferramenta(quebra)[1] is True
    assert cancelou(["Ajuste cancelado, nada foi salvo."]) and cancelou(["Saiu sem salvar."])
    assert not cancelou(["Rota salva: x=1 y=2 largura=3"])
    versoes_tentadas = []

    def comando_falso(cfg_, arq_, modelo, zoom, escala, site):
        versoes_tentadas.append(modelo.split("/tiles/")[1].split("/")[0])
        if "99.0" in modelo:
            sys.exit("Não deu para baixar o mapa: nenhum ladrilho encontrado")
        print("Mapa salvo.")

    cfg_arq = pe / "config.json"
    msg, erro = baixar_mapa_e_baus(PADRAO, cfg_arq, lambda: "99.0", comando_falso)
    assert versoes_tentadas == ["99.0", VERSAO_MAPA] and not erro and VERSAO_MAPA in msg  # versão nova sem ladrilhos
    versoes_tentadas.clear()

    def site_fora():
        raise OSError("sem rede")

    assert baixar_mapa_e_baus(PADRAO, cfg_arq, site_fora, comando_falso)[1] is False
    assert versoes_tentadas == [VERSAO_MAPA]
    assert baixar_mapa_e_baus(PADRAO, cfg_arq, lambda: VERSAO_MAPA, morre) == ("Não deu: sem rede", True)
    msg, erro = baixar_mapa_e_baus(PADRAO, cfg_arq, lambda: VERSAO_MAPA, fala)
    assert erro and msg.endswith("algo")  # mapa veio, baús não
    assert base_config(1.0, "rota").size == (CW * SS, CH * SS)
    sem_sair = copia(PADRAO); sem_sair["atalhos"]["sair"] = ["", ""]
    assert "Fechar contador" in validar(sem_sair) and validar(PADRAO) is None
    # desenho: painel em cada fase, quadro repetido não redesenha, janela de configuração
    t0 = 100.0
    c3, f3 = Contador(0.85, 3.0, 2689), Fase()
    anim = novo_anim(c3, 137500, t0)
    img = quadro(c3, t0, 1.0, "E", anim, texto_atalhos(PADRAO["atalhos"]), 137500, f3)
    transparente, faixa = ImageColor.getrgb(CHAVE), (20, (MUS_Y0 + MUS_Y1) // 2)
    assert img.size == (PW, ALTURA) and img.getpixel((0, 0)) == transparente  # canto fica transparente
    assert img.getpixel(faixa) == transparente                                # sem música: faixa some
    assert quadro(c3, t0 + 1, 1.0, "E", anim, "", 137500, f3) is None
    for base, fila in (("lobby", False), ("lobby", True), ("partida", False), ("fechado", False)):
        f3.base, f3.fila = base, fila
        assert quadro(c3, t0 + 2, 1.5, "E", anim, "", 137500, f3).size == (480, round(ALTURA * 1.5))
    # Spotify: título da janela -> música, e a faixa aparece no painel
    assert ler_titulo_spotify("Linkin Park - Numb") == ("Linkin Park", "Numb")
    assert ler_titulo_spotify("Queen - Bohemian Rhapsody - Remastered 2011") == \
        ("Queen", "Bohemian Rhapsody - Remastered 2011")
    assert all(ler_titulo_spotify(s) is None for s in ("Spotify", "Spotify Premium", "Spotify Free",
                                                       "Spotify - Advertisement", "GDI+ Window (Spotify.exe)", " - "))
    longa = ("Artista com um nome bem comprido", "Música com um nome tão comprido que não cabe na faixa")
    img = quadro(c3, t0 + 2.1, 1.0, "E", anim, "", 137500, f3, longa)
    assert img is not None and img.getpixel(faixa) != transparente
    assert quadro(c3, t0 + 3, 1.0, "E", anim, "", 137500, f3, longa) is None  # mesma música: não redesenha
    assert quadro(c3, t0 + 4, 1.0, "E", anim, "", 137500, f3).getpixel(faixa) == transparente  # pausou
    assert Pintor(10, 10, 1).caber("abcdef" * 20, 12, 60).endswith("…")
    if sys.platform == "win32":
        musica_spotify()  # chamadas do Windows não quebram (Spotify aberto ou não)
        cache_sp = {"janelas": [], "nomes": {}, "nomes_t": float("-inf"), "varredura": float("-inf")}
        titulos_spotify(cache_sp, 100.0)
        assert cache_sp["varredura"] == 100.0
        titulos_spotify(cache_sp, 102.0)                                       # janelas já achadas: sem varrer
        assert cache_sp["varredura"] == 100.0
        titulos_spotify(cache_sp, 131.0)
        assert cache_sp["varredura"] == 131.0                                  # a cada 30 s varre de novo
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


def memoria_mb():
    """(agora, pico) da memória do programa em MB; (0, 0) se não der para ler."""
    try:
        if sys.platform == "win32":
            from ctypes import wintypes

            class Contadores(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + \
                    [(n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                                     "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                                     "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
            c = Contadores()
            c.cb = ctypes.sizeof(c)
            psapi, k32 = ctypes.WinDLL("psapi"), ctypes.WinDLL("kernel32")
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.POINTER(Contadores), wintypes.DWORD)
            if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return c.WorkingSetSize / 2 ** 20, c.PeakWorkingSetSize / 2 ** 20
            return 0.0, 0.0
        campos = dict(linha.split(":", 1) for linha in Path("/proc/self/status").read_text().splitlines() if ":" in linha)
        return int(campos["VmRSS"].split()[0]) / 1024, int(campos["VmHWM"].split()[0]) / 1024
    except (OSError, KeyError, ValueError, AttributeError):
        return 0.0, 0.0


class Medidor:
    """Modo --medir: quanto o programa pesa no PC (CPU, memória e cada parte), para o desempenho.txt."""

    def __init__(self, segundos, relogio=time.monotonic, cpu=time.process_time, nucleos=None):
        self.segundos, self.relogio, self.cpu = segundos, relogio, cpu
        self.nucleos = nucleos or os.cpu_count() or 1
        self.t0, self.cpu0 = relogio(), cpu()
        self.amostras = [(self.t0, self.cpu0)]  # (relógio, cpu) a cada 5 s
        self.partes = {}                         # parte -> [vezes, ms total, ms pior]
        self.fases, self._ult = {}, self.t0
        self.pico_mb = 0.0

    def anotar(self, parte, ms):
        v = self.partes.setdefault(parte, [0, 0.0, 0.0])
        v[0] += 1
        v[1] += ms
        v[2] = max(v[2], ms)

    def tick(self, t, fase):
        self.fases[fase] = self.fases.get(fase, 0.0) + t - self._ult
        self._ult = t
        if t - self.amostras[-1][0] >= 5:
            self.amostras.append((t, self.cpu()))
            self.pico_mb = max(self.pico_mb, memoria_mb()[1])
        return t - self.t0 >= self.segundos

    def relatorio(self):
        t, cpu = self.relogio(), self.cpu()
        dur = max(1e-6, t - self.t0)
        media = (cpu - self.cpu0) / dur * 100  # % de um núcleo
        pior = max(((c1 - c0) / max(1e-6, t1 - t0) * 100 for (t0, c0), (t1, c1) in
                    zip(self.amostras, self.amostras[1:] + [(t, cpu)])), default=media)
        agora_mb, pico_mb = memoria_mb()
        pico_mb = max(pico_mb, self.pico_mb)
        no_pc, pior_pc = media / self.nucleos, pior / self.nucleos
        veredito = "LEVE" if no_pc <= 2 and pico_mb <= 350 else "OK" if no_pc <= 5 and pico_mb <= 600 else "PESADO"
        linhas = [f"ChestTracker - teste de desempenho ({dur:.0f} s, {time.strftime('%Y-%m-%d %H:%M')})",
                  f"PC: {self.nucleos} núcleos | {sys.platform}", "",
                  f"VEREDITO: {veredito}  (leve = até 2% do PC na média e até 350 MB de memória)", "",
                  f"CPU do programa: {no_pc:.1f}% do PC na média ({media:.0f}% de um núcleo)",
                  f"Pior trecho de 5 s: {pior_pc:.1f}% do PC ({pior:.0f}% de um núcleo)",
                  f"Memória: {agora_mb:.0f} MB agora, pico {pico_mb:.0f} MB", "", "Cada parte (vezes | média | pior):"]
        for parte, (n, total, maximo) in sorted(self.partes.items()):
            linhas.append(f"  {parte:<22} {n:>6} | {total / n:7.1f} ms | {maximo:7.1f} ms")
        linhas += ["", "Tempo em cada fase do jogo:"] + \
            [f"  {FASES.get(f, (str(f), ''))[0].lower():<22} {s:6.0f} s" for f, s in sorted(self.fases.items(), key=lambda x: -x[1])]
        return "\n".join(linhas) + "\n"


_ARQ_CRASH = None


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
    try:  # queda fora do Python (DLL, driver): grava onde cada thread estava
        global _ARQ_CRASH
        _ARQ_CRASH = open(arquivo.with_name("chesttracker_crash.log"), "a", encoding="utf-8")
        _ARQ_CRASH.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} iniciado\n")
        _ARQ_CRASH.flush()
        faulthandler.enable(_ARQ_CRASH, all_threads=True)
    except OSError as e:
        log.warning("Sem registro de crash (%s)", e)
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
    p.add_argument("--medir", type=float, metavar="SEGUNDOS",
                   help="teste de desempenho: roda normal e, depois desse tempo, grava desempenho.txt e fecha")
    p.add_argument("--testar-roda", action="store_true",
                   help="diagnóstico: confere se a leitura da rodinha do mouse funciona e grava roda_teste.txt")
    p.add_argument("--sem-spotify", action="store_true", help="não mostra a música do Spotify, mesmo ligada na janela")
    p.add_argument("--debug", action="store_true", help="log detalhado")
    p.add_argument("--autoteste", action="store_true", help="roda os testes da lógica e sai")
    p.add_argument("--editar-rota", metavar="NOME", nargs="?", const="rota1",
                   help="abre o editor para desenhar (ou alterar) a rota NOME em cima do mapa")
    p.add_argument("--baixar-mapa", metavar="MODELO",
                   help="baixa o mapa em ladrilhos (endereço com {z}, {x} e {y}) e salva como mapa.jpg")
    p.add_argument("--zoom", type=int, default=4, help="zoom dos ladrilhos do --baixar-mapa (padrão 4)")
    p.add_argument("--escala", type=float, default=0.0,
                   help="com --baixar-mapa: px do mapa por px do minimapa, se você já souber (0 = descobrir)")
    p.add_argument("--baus-de", metavar="SITE", default="",
                   help="com --baixar-mapa: site de onde baixar a posição dos baús (ex.: https://chestroute.com)")
    p.add_argument("--converter-rotas", metavar="IMAGEM,FATOR,DX,DY", default="",
                   help=argparse.SUPPRESS)
    p.add_argument("--marcar-minimapa", action="store_true",
                   help="marca onde o minimapa fica na tela, para a rota acompanhar o personagem")
    args = p.parse_args()

    configurar_log(args.debug)

    if Image is None:
        sys.exit("Falta o Pillow: rode  pip install pillow")
    if args.autoteste:
        autoteste()
        return
    if args.testar_roda:
        if sys.platform != "win32":
            sys.exit("O teste da rodinha é só para Windows.")
        sys.exit(0 if testar_roda() else 1)
    ativar_dpi()  # antes de abrir qualquer janela

    cfg = carregar_config(args.config)
    if args.baixar_mapa:
        comando_baixar_mapa(cfg, args.config, args.baixar_mapa.strip(), args.zoom, max(0.0, min(args.escala, 20.0)),
                            args.baus_de.strip(), args.converter_rotas.strip())
        return
    if args.editar_rota:
        editar_rota(cfg, args.config, args.editar_rota.strip())
        return
    if args.marcar_minimapa:
        marcar_minimapa(cfg, args.config)
        return
    try:
        pagina, aviso = "principal", None
        while not args.direto:
            escolha = configurar(cfg, args.config, pagina, aviso)
            if escolha is None:
                log.info("Janela de configuração fechada sem iniciar")
                return
            cfg = escolha[1]
            if escolha[0] == "iniciar":
                break
            aviso = rodar_ferramenta_rota(escolha[0], cfg, args.config, escolha[2])
            cfg["rota"], pagina = rota_do_arquivo(args.config), "rota"
        inicial = 0 if args.zerar else carregar(args.arquivo)
        cont = Contador(cfg["min"], cfg["max"], inicial)
        estado, fase = {"sair": False, "musica": None}, Fase()
        log.info("Iniciado com %d baús | conta entre %.2fs e %.2fs | atalhos: %s",
                 inicial, cfg["min"], cfg["max"], texto_atalhos(cfg["atalhos"]))
        iniciar_entrada(cont, cfg, args.arquivo, estado, fase)
        iniciar_fase(fase, cont)
        medidor = Medidor(args.medir) if args.medir else None
        if sys.platform == "win32" and cfg["spotify"] and not args.sem_spotify:
            iniciar_spotify(estado, medidor=medidor)
    except ImportError:
        sys.exit("Falta o pynput: rode  pip install pynput")

    try:
        overlay(cont, cfg, estado, fase, args.config, medidor)
    except KeyboardInterrupt:
        pass
    finally:
        salvar(args.arquivo, cont.total)
        log.info("Encerrado com %d baús (salvo em %s)", cont.total, args.arquivo)


if __name__ == "__main__":
    main()
