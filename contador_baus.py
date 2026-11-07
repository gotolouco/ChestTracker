#!/usr/bin/env python3
"""ChestTracker: overlay que conta os baús do Fortnite pelo tempo segurando a tecla de interação.

Abre uma janela para escolher atalhos e ajustes (salvos em config.json) e depois o overlay.
Requisito: pip install pynput   |   Fortnite em "Tela cheia em janela" (sem borda).
"""
import argparse
import ctypes
import json
import logging
import math
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("baus")
BAUS_JA_ABERTOS = 1000  # baús abertos antes do contador existir
# No .exe (PyInstaller) o __file__ fica numa pasta temporária apagada ao fechar; salva ao lado do .exe.
PASTA = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
RECURSOS = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))  # arquivos embutidos no .exe


class Contador:
    """Conta 1 baú quando a tecla fica segurada entre `minimo` e `maximo` segundos."""

    def __init__(self, minimo, maximo, inicial=0):
        self.minimo, self.maximo = minimo, maximo
        self.total = inicial
        self.sessao = 0      # baús desde que o contador abriu
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
                return True
            return False

    def ajustar(self, delta):
        with self.lock:
            self.total = max(0, self.total + delta)
            self.sessao = max(0, self.sessao + delta)

    def zerar(self):
        with self.lock:
            self.total = 0

    def segurando(self, t):
        with self.lock:
            return None if self.inicio is None else t - self.inicio


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
    """Deixa o mouse passar pela janela e mantém o botão na barra de tarefas (Windows)."""
    GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT = -20, 0x80000, 0x20
    WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = 0x80, 0x40000
    u32 = ctypes.windll.user32
    hwnd = u32.GetParent(root.winfo_id())
    estilo = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    estilo = (estilo | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_APPWINDOW) & ~WS_EX_TOOLWINDOW
    u32.SetWindowLongW(hwnd, GWL_EXSTYLE, estilo)
    # janela sem borda (overrideredirect) só ganha botão na barra depois de esconder e mostrar de novo
    root.withdraw()
    root.after(10, root.deiconify)


# Atalhos: cada ação tem 2 espaços; "" = vazio. Nomes: letra ("e"), tecla do pynput ("f8") ou "mouse3/4/5".
PADRAO = {"atalhos": {"segurar": ["e", ""], "somar": ["f8", "mouse4"], "descontar": ["f7", ""],
                      "zerar": ["f9", ""], "sair": ["f10", ""]},
          "min": 0.85, "max": 3.0, "escala": 1.0, "x": 30, "y": 30}
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
    for chave in ("min", "max", "escala", "x", "y"):
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

    def __init__(self, cont, atalhos, ao_mudar, ao_sair):
        self.cont, self.ao_mudar, self.ao_sair = cont, ao_mudar, ao_sair
        self.acoes = {n: acao for acao, nomes in atalhos.items() for n in nomes if n}
        self.apertadas = set()

    def descer(self, nome, t):
        if nome is None or nome in self.apertadas:
            return  # auto-repeat de tecla segurada
        self.apertadas.add(nome)
        acao = self.acoes.get(nome)
        if acao == "segurar":
            self.cont.press(t)
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


def iniciar_entrada(cont, cfg, arquivo, estado):
    from pynput import keyboard, mouse

    def mudou():
        log.info("Total %d", cont.total)
        salvar(arquivo, cont.total)

    e = Entrada(cont, cfg["atalhos"], mudou, lambda: estado.update(sair=True))

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
FONTES = ("Burbank Big Condensed Black", "Burbank Big Condensed", "Impact", "Arial Black")

# Baú em pixel art 16x16: o mesmo desenho do ícone do programa.
BAU_CORES = {"k": "#1A0F05", "h": "#FFE58A", "l": "#FFC93C", "d": "#8B5A12", "b": "#E8A317",
             "s": "#B97C0E", "w": "#FFF3B0", "o": "#5A3A0A"}
_MEIO = ("hhdhhhhhhdhh", "lldlllllldll", "lldlkkkkldll", "kkkkwwwwkkkk", "bbdkwoowkdbb",
         "bbdkwwwwkdbb", "bbdbkkkkbdbb", "bbdbbbbbbdbb", "bbdbbbbbbdbb", "ssdssssssdss")
BAU = (("." * 16,) * 2 + ("..kkkkkkkkkkkk..",) + tuple(".k" + m + "k." for m in _MEIO)
       + ("..kkkkkkkkkkkk..",) + ("." * 16,) * 2)


def misturar(a, b, f):
    """Cor entre a e b (f de 0 a 1), para o degradê do painel."""
    ca, cb = (int(a[i:i + 2], 16) for i in (1, 3, 5)), (int(b[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{round(x + (y - x) * f):02X}" for x, y in zip(ca, cb))


def texto(cv, x, y, s, fonte, cor, borda, anchor="w"):
    """Texto com contorno preto, legível em cima de qualquer cena do jogo."""
    for dx, dy in ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)):
        cv.create_text(x + dx * borda, y + dy * borda, text=s, font=fonte, fill=PRETO, anchor=anchor)
    cv.create_text(x, y, text=s, font=fonte, fill=cor, anchor=anchor)


def desenhar_bau(cv, cx, cy, c):
    """Desenha BAU centrado em (cx, cy), cada pixel com c px; junta pixels iguais em faixas."""
    ox, oy = cx - 8 * c, cy - 8 * c
    for j, linha in enumerate(BAU):
        i = 0
        while i < 16:
            f = i
            while f < 16 and linha[f] == linha[i]:
                f += 1
            if linha[i] != ".":
                cv.create_rectangle(round(ox + i * c), round(oy + j * c), round(ox + f * c),
                                    round(oy + (j + 1) * c), fill=BAU_CORES[linha[i]], outline="")
            i = f


def desenhar(cv, cont, t, fam, k, tecla, anim, atalhos):
    cv.delete("all")
    W, H, sk = 320*k, 136*k, 16*k  # sk = inclinação do painel

    def faixa(y0, y1, cor, x0=0, x1=W):
        """Polígono inclinado igual aos painéis do jogo."""
        cv.create_polygon(x0 + sk*(1 - y0/H), y0, x1 - sk*y0/H, y0,
                          x1 - sk*y1/H, y1, x0 + sk*(1 - y1/H), y1, fill=cor, outline="")

    passos, corpo = 14, H - 24*k
    for i in range(passos):  # degradê de cima (azul vivo) para baixo (azul noite)
        faixa(corpo * i / passos, corpo * (i + 1) / passos + 1, misturar(AZUL_TOPO, AZUL_NOITE, i / (passos - 1)))
    faixa(0, 3*k, CIANO)
    faixa(H - 24*k, H, AZUL_FAIXA)
    faixa(H - 25*k, H - 23*k, AMARELO)
    faixa(0, H, AMARELO, x1=sk + 10*k)

    # "pop" no número, raios dourados e "+1" quando um baú entra
    if cont.total > anim["visto"]:
        anim["pop"] = t
    anim["visto"] = cont.total
    dt = t - anim["pop"]
    cx, cy, r, w = 74*k, 44*k, 32*k, max(2, round(7*k))
    if dt < 0.6:
        alcance, giro = r * (1.45 - 0.35 * dt / 0.6), dt * 90
        for n in range(10):
            a = math.radians(giro + n * 36)
            pontas = [(cx + alcance * math.cos(a + d), cy + alcance * math.sin(a + d)) for d in (-0.16, 0.16)]
            cv.create_polygon(cx, cy, *pontas[0], *pontas[1], fill=AMARELO if n % 2 else "#FFB000", outline="")

    # anel de interação em volta do baú (igual ao "segure E" do jogo)
    cv.create_oval(cx - r, cy - r, cx + r, cy + r, fill=AZUL_NOITE, outline=ANEL, width=w)
    s = cont.segurando(t)
    if s is not None:
        cor = VERMELHO if s > cont.maximo else VERDE if s >= cont.minimo else BRANCO
        frac = min(s / cont.minimo, 1)
        cv.create_arc(cx - r, cy - r, cx + r, cy + r, start=90, extent=-359.9*frac,
                      style="arc", outline=cor, width=w)
    salto = 4*k * max(0.0, 1 - dt / 0.25)  # baú pula junto com o número
    desenhar_bau(cv, cx, cy - salto, 3.6*k)

    escala = 1 + 0.2 * max(0.0, 1 - dt / 0.3)
    texto(cv, 222*k, 22*k, "BAÚS ABERTOS", (fam, -round(17*k)), BRANCO, max(1, round(2*k)), "center")
    numero = f"{cont.total:,}".replace(",", ".")
    texto(cv, 222*k, 60*k, numero, (fam, -round(50*k*escala)), AMARELO, max(2, round(3*k)), "center")
    if dt < 0.7:
        texto(cv, cx + r, cy - r + 16*k - 20*k*dt, "+1", (fam, -round(26*k)), VERDE, max(1, round(2*k)), "center")

    # linha da sessão: baús desde que abriu + ritmo
    fonte_sessao, b = (fam, -round(16*k)), max(1, round(2*k))
    texto(cv, 26*k, H - 38*k, f"+{cont.sessao} NESTA SESSÃO", fonte_sessao, VERDE, b)
    ritmo = por_hora(cont.sessao, t - anim["inicio"])
    texto(cv, W - sk - 12*k, H - 38*k, f"{'--' if ritmo is None else ritmo} BAÚS/H",
          fonte_sessao, CIANO, b, "e")

    # rodapé: estado do hold ou atalhos
    if s is not None:
        rodape, cor = f"SEGURANDO {tecla}  {s:.2f}s / {cont.minimo:.2f}s", cor
    elif cont.ultima is not None:
        rodape, cor = f"ÚLTIMO HOLD: {cont.ultima:.2f}s", CINZA
    else:
        rodape, cor = atalhos, CIANO
    texto(cv, 26*k, H - 12*k, rodape, (fam, -round(14*k)), cor, 1)


def colocar_icone(root):
    icone = RECURSOS / "assets" / "chesttracker.ico"
    if sys.platform == "win32" and icone.exists():
        root.iconbitmap(default=str(icone))


def escolher_fonte(root):
    from tkinter import font as tkfont
    familias = set(tkfont.families(root))
    return next((f for f in FONTES if f in familias), "Arial")


def configurar(cfg, arquivo_cfg):
    """Janela antes do overlay: atalhos, tempos e painel. Devolve a config escolhida ou None se fechar."""
    import queue
    import tkinter as tk
    from pynput import keyboard, mouse

    cfg = copia(cfg)  # só vale se clicar em INICIAR
    root = tk.Tk()
    root.title("ChestTracker")
    root.configure(bg=AZUL_NOITE, padx=20, pady=14)
    root.resizable(False, False)
    colocar_icone(root)
    fam = escolher_fonte(root)
    normal = ("Segoe UI", 10)

    topo = tk.Canvas(root, width=380, height=60, bg=AZUL_NOITE, highlightthickness=0)
    topo.grid(row=0, column=0, columnspan=3, sticky="w")
    desenhar_bau(topo, 28, 30, 3.2)
    texto(topo, 62, 30, "CHESTTRACKER", (fam, -34), AMARELO, 2)

    def secao(linha, nome):
        tk.Label(root, text=nome, font=(fam, -18), fg=CIANO, bg=AZUL_NOITE).grid(
            row=linha, column=0, columnspan=3, sticky="w", pady=(12, 4))

    botoes, captura, fila = {}, {"alvo": None, "ouvintes": []}, queue.Queue()
    status = tk.StringVar(value="Clique num atalho e aperte a tecla ou o botão do mouse.")

    def pintar():
        for (acao, i), b in botoes.items():
            nome = cfg["atalhos"][acao][i]
            b.config(text=exibir(nome), bg=AZUL_TOPO if nome else ANEL, fg=BRANCO if nome else CINZA)

    def parar():
        for o in captura["ouvintes"]:
            o.stop()
        captura.update(alvo=None, ouvintes=[])

    def capturar(acao, i):
        parar()
        pintar()
        root.focus_set()  # Backspace não pode apagar texto de um campo
        captura["alvo"] = (acao, i)
        botoes[(acao, i)].config(text="APERTE...", bg=AMARELO, fg=PRETO)
        status.set("Aperte a tecla ou o botão do mouse (meio, 4 ou 5). Esc cancela, Backspace limpa.")

        def clique(x, y, botao, apertou):  # sem return: False pararia o listener
            if apertou:
                fila.put(nome_botao(botao))

        k = keyboard.Listener(on_press=lambda key: fila.put(nome_tecla(key)))
        m = mouse.Listener(on_click=clique)
        k.start()
        m.start()
        captura["ouvintes"] = [k, m]

    def receber():
        while not fila.empty():
            nome = fila.get_nowait()
            if captura["alvo"] is None or nome is None:  # clique esquerdo/direito: continua esperando
                continue
            acao, i = captura["alvo"]
            parar()
            if nome == "esc":
                status.set("Cancelado.")
            elif nome == "backspace":
                cfg["atalhos"][acao][i] = ""
                status.set(f"{ROTULOS[acao]}: atalho removido.")
            else:
                status.set(atribuir(cfg["atalhos"], acao, i, nome))
            pintar()
        root.after(30, receber)

    secao(1, "ATALHOS")
    for n, (acao, rotulo) in enumerate(ACOES, start=2):
        tk.Label(root, text=rotulo, font=normal, fg=BRANCO, bg=AZUL_NOITE, anchor="w", width=18).grid(
            row=n, column=0, sticky="w", pady=2)
        for i in (0, 1):
            b = tk.Button(root, command=lambda a=acao, j=i: capturar(a, j), font=(fam, -15), width=11,
                          relief="flat", bd=0, takefocus=0, cursor="hand2",
                          activebackground=AMARELO, activeforeground=PRETO)
            b.grid(row=n, column=1 + i, padx=3, pady=2, ipady=2)
            botoes[(acao, i)] = b

    secao(7, "AJUSTES")
    campos = {}

    def campo(linha, coluna, chave, rotulo, de, ate, passo, fmt):
        quadro = tk.Frame(root, bg=AZUL_NOITE)
        quadro.grid(row=linha, column=coluna, sticky="w", pady=2, padx=(0, 6))
        tk.Label(quadro, text=rotulo, font=normal, fg=CINZA, bg=AZUL_NOITE).pack(anchor="w")
        var = tk.StringVar(value=fmt % cfg[chave])
        tk.Spinbox(quadro, from_=de, to=ate, increment=passo, format=fmt, textvariable=var, width=9,
                   font=normal, relief="flat", bg=BRANCO, buttonbackground=AZUL_TOPO).pack(anchor="w")
        var.set(fmt % cfg[chave])  # o Spinbox troca pelo valor inicial dele ao ser criado
        campos[chave] = (var, fmt)

    campo(8, 0, "min", "Tempo mínimo (s)", 0.1, 10, 0.05, "%.2f")
    campo(8, 1, "max", "Tempo máximo (s)", 0.2, 30, 0.1, "%.2f")
    campo(8, 2, "escala", "Tamanho do painel", 0.3, 5, 0.1, "%.1f")
    campo(9, 0, "x", "Posição X (px)", 0, 7680, 10, "%.0f")
    campo(9, 1, "y", "Posição Y (px)", 0, 4320, 10, "%.0f")

    tk.Label(root, textvariable=status, font=normal, fg=AMARELO, bg=AZUL_NOITE, wraplength=380,
             justify="left", anchor="nw", height=2)  # 2 linhas fixas: a janela não muda de tamanho.grid(row=10, column=0, columnspan=3, sticky="we", pady=(12, 8))

    resultado = {"cfg": None}

    def iniciar(_evento=None):
        if captura["alvo"]:
            return  # Enter durante a captura vira atalho, não clique em INICIAR
        novo = copia(cfg)
        try:
            for chave, (var, _) in campos.items():
                v = float(var.get().replace(",", "."))
                novo[chave] = round(v) if chave in ("x", "y") else v
        except ValueError:
            status.set("Algum número está inválido. Use só números, com ponto ou vírgula.")
            return
        erro = validar(novo)
        if erro:
            status.set(erro)
            return
        salvar_json(arquivo_cfg, novo)
        resultado["cfg"] = novo
        parar()
        root.destroy()

    def restaurar():
        parar()
        cfg.clear()
        cfg.update(copia(PADRAO))
        for chave, (var, fmt) in campos.items():
            var.set(fmt % cfg[chave])
        pintar()
        status.set("Atalhos e ajustes voltaram ao padrão.")

    def fechar():
        parar()
        root.destroy()

    tk.Button(root, text="Restaurar padrão", command=restaurar, font=normal, relief="flat", bd=0,
              bg=ANEL, fg=CINZA, activebackground=AZUL_TOPO, cursor="hand2").grid(
        row=11, column=0, sticky="w", ipadx=8, ipady=4)
    tk.Button(root, text="INICIAR", command=iniciar, font=(fam, -22), relief="flat", bd=0,
              bg=AMARELO, fg=PRETO, activebackground="#FFB000", cursor="hand2").grid(
        row=11, column=1, columnspan=2, sticky="we", padx=3, ipady=4)

    root.bind("<Return>", iniciar)
    root.protocol("WM_DELETE_WINDOW", fechar)
    pintar()
    receber()
    root.mainloop()
    return resultado["cfg"]


def overlay(cont, cfg, estado):
    import tkinter as tk

    k = cfg["escala"]
    root = tk.Tk()
    root.title("ChestTracker")  # nome do botão na barra de tarefas
    colocar_icone(root)
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=CHAVE)
    if sys.platform == "win32":
        root.attributes("-transparentcolor", CHAVE)
    root.geometry(f"+{cfg['x']}+{cfg['y']}")
    cv = tk.Canvas(root, width=round(324*k), height=round(138*k), bg=CHAVE, highlightthickness=0)
    cv.pack()

    fam = escolher_fonte(root)
    log.debug("Fonte do overlay: %s", fam)

    root.update_idletasks()
    if sys.platform == "win32":
        clique_atravessa(root)

    tecla = exibir(next((n for n in cfg["atalhos"]["segurar"] if n), ""))
    atalhos = texto_atalhos(cfg["atalhos"])
    anim = {"visto": cont.total, "pop": float("-inf"), "inicio": time.monotonic()}

    def tick():
        if estado["sair"]:
            root.destroy()
            return
        desenhar(cv, cont, time.monotonic(), fam, k, tecla, anim, atalhos)
        root.attributes("-topmost", True)  # o jogo às vezes rouba o topo
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
    sem_sair = copia(PADRAO); sem_sair["atalhos"]["sair"] = ["", ""]
    assert "Fechar contador" in validar(sem_sair) and validar(PADRAO) is None
    print("autoteste ok")


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

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if args.autoteste:
        autoteste()
        return

    cfg = carregar_config(args.config)
    try:
        if not args.direto:
            cfg = configurar(cfg, args.config)
            if cfg is None:
                log.info("Janela de configuração fechada sem iniciar")
                return
        inicial = 0 if args.zerar else carregar(args.arquivo)
        cont = Contador(cfg["min"], cfg["max"], inicial)
        estado = {"sair": False}
        log.info("Iniciado com %d baús | conta entre %.2fs e %.2fs | atalhos: %s",
                 inicial, cfg["min"], cfg["max"], texto_atalhos(cfg["atalhos"]))
        iniciar_entrada(cont, cfg, args.arquivo, estado)
    except ImportError:
        sys.exit("Falta o pynput: rode  pip install pynput")

    try:
        overlay(cont, cfg, estado)
    except KeyboardInterrupt:
        pass
    finally:
        salvar(args.arquivo, cont.total)
        log.info("Encerrado com %d baús (salvo em %s)", cont.total, args.arquivo)


if __name__ == "__main__":
    main()
