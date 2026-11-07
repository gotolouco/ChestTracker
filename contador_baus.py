#!/usr/bin/env python3
"""Contador de baús do Fortnite: overlay na tela + contagem pelo tempo segurando E.

Atalhos:  F8 ou Mouse 4 = +1   F7 = -1   F9 = zerar   F10 = sair
Requisito: pip install pynput   |   Fortnite em "Tela cheia em janela" (sem borda).
"""
import argparse
import ctypes
import json
import logging
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("baus")
BAUS_JA_ABERTOS = 1000  # baús abertos antes do contador existir
# No .exe (PyInstaller) o __file__ fica numa pasta temporária apagada ao fechar; salva ao lado do .exe.
PASTA = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent


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
    tmp = arquivo.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps({"baus": total}), encoding="utf-8")
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


def iniciar_teclado(cont, args, estado):
    from pynput import keyboard, mouse

    alvo = args.tecla.lower()

    def eh_alvo(key):
        c = getattr(key, "char", None)
        vk = getattr(key, "vk", None)
        return (c is not None and c.lower() == alvo) or (alvo.isalnum() and vk == ord(alvo.upper()))

    def on_press(key):
        if eh_alvo(key):
            cont.press(time.monotonic())

    def on_release(key):
        if eh_alvo(key):
            contou = cont.release(time.monotonic())
            log.info("Segurou %s por %.2fs -> %s (total %d)", args.tecla.upper(), cont.ultima,
                     "BAÚ +1" if contou else "ignorado", cont.total)
            if contou:
                salvar(args.arquivo, cont.total)
            return
        acoes = {keyboard.Key.f8: lambda: cont.ajustar(+1),
                 keyboard.Key.f7: lambda: cont.ajustar(-1),
                 keyboard.Key.f9: cont.zerar}
        if key in acoes:
            acoes[key]()
            log.info("Ajuste manual -> total %d", cont.total)
            salvar(args.arquivo, cont.total)
        elif key == keyboard.Key.f10:
            estado["sair"] = True
            return False  # para o listener

    mouse4 = getattr(mouse.Button, "x1", None)  # botão lateral "voltar"; só existe no Windows

    def on_click(x, y, botao, apertou):
        if apertou and botao == mouse4:
            cont.ajustar(+1)
            log.info("Mouse 4 -> total %d", cont.total)
            salvar(args.arquivo, cont.total)

    mouse.Listener(on_click=on_click).start()  # já é daemon
    ouvinte = keyboard.Listener(on_press=on_press, on_release=on_release)
    ouvinte.start()
    return ouvinte


# Paleta do HUD do Fortnite: azul profundo, amarelo lendário, contorno preto grosso.
CHAVE = "#010101"  # cor que vira transparente
AMARELO, BRANCO, PRETO = "#FFD84D", "#FFFFFF", "#000000"
AZUL_NOITE, AZUL, AZUL_FAIXA, CIANO = "#0A1640", "#1B3FA0", "#050B22", "#3FD0FF"
VERDE, VERMELHO, CINZA = "#7CFC00", "#FF4D4D", "#AEB8D6"
FONTES = ("Burbank Big Condensed Black", "Burbank Big Condensed", "Impact", "Arial Black")


def texto(cv, x, y, s, fonte, cor, borda, anchor="w"):
    """Texto com contorno preto, legível em cima de qualquer cena do jogo."""
    for dx, dy in ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)):
        cv.create_text(x + dx * borda, y + dy * borda, text=s, font=fonte, fill=PRETO, anchor=anchor)
    cv.create_text(x, y, text=s, font=fonte, fill=cor, anchor=anchor)


def desenhar_bau(cv, cx, cy, k):
    b = max(1, round(2 * k))
    cv.create_rectangle(cx - 22*k, cy - 3*k, cx + 22*k, cy + 18*k, fill="#E8A317", outline=PRETO, width=b)
    cv.create_rectangle(cx - 22*k, cy - 17*k, cx + 22*k, cy - 3*k, fill="#FFC93C", outline=PRETO, width=b)
    for x in (cx - 15*k, cx + 11*k):  # cintas de metal
        cv.create_rectangle(x, cy - 17*k, x + 4*k, cy + 18*k, fill="#8B5A12", outline=PRETO, width=b)
    cv.create_rectangle(cx - 5*k, cy - 8*k, cx + 5*k, cy + 4*k, fill="#FFF3B0", outline=PRETO, width=b)


def desenhar(cv, cont, t, fam, k, tecla, anim):
    cv.delete("all")
    W, H, sk = 320*k, 136*k, 16*k  # sk = inclinação do painel

    def faixa(y0, y1, cor, x0=0, x1=W):
        """Polígono inclinado igual aos painéis do jogo."""
        cv.create_polygon(x0 + sk*(1 - y0/H), y0, x1 - sk*y0/H, y0,
                          x1 - sk*y1/H, y1, x0 + sk*(1 - y1/H), y1, fill=cor, outline="")

    faixa(0, H, AZUL_NOITE)
    faixa(0, 50*k, AZUL)
    faixa(0, 3*k, CIANO)
    faixa(H - 24*k, H, AZUL_FAIXA)
    faixa(0, H, AMARELO, x1=sk + 10*k)

    # anel de interação em volta do baú (igual ao "segure E" do jogo)
    cx, cy, r, w = 74*k, 44*k, 32*k, max(2, round(7*k))
    cv.create_oval(cx - r, cy - r, cx + r, cy + r, outline="#24305A", width=w)
    s = cont.segurando(t)
    if s is not None:
        cor = VERMELHO if s > cont.maximo else VERDE if s >= cont.minimo else BRANCO
        frac = min(s / cont.minimo, 1)
        cv.create_arc(cx - r, cy - r, cx + r, cy + r, start=90, extent=-359.9*frac,
                      style="arc", outline=cor, width=w)
    desenhar_bau(cv, cx, cy + 2*k, k)

    # "pop" no número e "+1" subindo quando um baú entra
    if cont.total > anim["visto"]:
        anim["pop"] = t
    anim["visto"] = cont.total
    dt = t - anim["pop"]
    escala = 1 + 0.2 * max(0.0, 1 - dt / 0.3)
    texto(cv, 222*k, 22*k, "BAÚS ABERTOS", (fam, -round(17*k)), BRANCO, max(1, round(2*k)), "center")
    numero = f"{cont.total:,}".replace(",", ".")
    texto(cv, 222*k, 60*k, numero, (fam, -round(50*k*escala)), AMARELO, max(2, round(3*k)), "center")
    if dt < 0.7:
        texto(cv, cx + r, cy - r + 8*k - 30*k*dt, "+1", (fam, -round(26*k)), VERDE, max(1, round(2*k)), "center")

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
        rodape, cor = "F8/M4 +1   F7 -1   F9 ZERA   F10 SAI", CIANO
    texto(cv, 26*k, H - 12*k, rodape, (fam, -round(14*k)), cor, 1)


def overlay(cont, args, estado):
    import tkinter as tk
    from tkinter import font as tkfont

    k = args.escala
    root = tk.Tk()
    root.title("ChestTracker")  # nome do botão na barra de tarefas
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=CHAVE)
    if sys.platform == "win32":
        root.attributes("-transparentcolor", CHAVE)
    root.geometry(f"+{args.x}+{args.y}")
    cv = tk.Canvas(root, width=round(324*k), height=round(138*k), bg=CHAVE, highlightthickness=0)
    cv.pack()

    familias = set(tkfont.families(root))
    fam = next((f for f in FONTES if f in familias), "Arial")
    log.debug("Fonte do overlay: %s", fam)

    root.update_idletasks()
    if sys.platform == "win32":
        clique_atravessa(root)

    tecla = args.tecla.upper()
    anim = {"visto": cont.total, "pop": float("-inf"), "inicio": time.monotonic()}

    def tick():
        if estado["sair"]:
            root.destroy()
            return
        desenhar(cv, cont, time.monotonic(), fam, k, tecla, anim)
        root.attributes("-topmost", True)  # o jogo às vezes rouba o topo
        root.after(33, tick)

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
    assert por_hora(5, 60) is None                   # aquecimento
    assert por_hora(30, 1800) == 60 and por_hora(0, 600) == 0
    print("autoteste ok")


def main():
    p = argparse.ArgumentParser(description="Overlay contador de baús do Fortnite")
    p.add_argument("--min", type=float, default=0.85, help="segundos mínimos segurando para contar (padrão 0.85; baú abre em ~0.95s)")
    p.add_argument("--max", type=float, default=3.0, help="segundos máximos; acima disso ignora (ex.: reviver) (padrão 3.0)")
    p.add_argument("--tecla", default="e", help="tecla de interação (padrão e)")
    p.add_argument("--x", type=int, default=30, help="posição X do overlay (padrão 30)")
    p.add_argument("--y", type=int, default=30, help="posição Y do overlay (padrão 30)")
    p.add_argument("--escala", type=float, default=1.0, help="tamanho do overlay (padrão 1.0; ex.: 1.5 em tela 2K/4K)")
    p.add_argument("--arquivo", type=Path, default=PASTA / "baus.json",
                   help="onde salvar a contagem (padrão baus.json ao lado do script/.exe)")
    p.add_argument("--zerar", action="store_true", help="começa a contagem do 0")
    p.add_argument("--debug", action="store_true", help="log detalhado")
    p.add_argument("--autoteste", action="store_true", help="roda os testes da lógica e sai")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    if args.autoteste:
        autoteste()
        return
    if not 0 < args.min < args.max:
        p.error("precisa 0 < --min < --max")
    if not 0.3 <= args.escala <= 5:
        p.error("--escala deve ficar entre 0.3 e 5")
    if len(args.tecla) != 1:
        p.error("--tecla deve ser um único caractere")

    inicial = 0 if args.zerar else carregar(args.arquivo)
    cont = Contador(args.min, args.max, inicial)
    estado = {"sair": False}
    log.info("Iniciado com %d baús | conta entre %.2fs e %.2fs segurando %s | F7 -1, F8 +1, F9 zera, F10 sai",
             inicial, args.min, args.max, args.tecla.upper())

    try:
        iniciar_teclado(cont, args, estado)
    except ImportError:
        sys.exit("Falta o pynput: rode  pip install pynput")

    try:
        overlay(cont, args, estado)
    except KeyboardInterrupt:
        pass
    finally:
        salvar(args.arquivo, cont.total)
        log.info("Encerrado com %d baús (salvo em %s)", cont.total, args.arquivo)


if __name__ == "__main__":
    main()
