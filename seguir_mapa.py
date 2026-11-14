#!/usr/bin/env python3
"""Descobre onde o jogador está no mapa olhando só o minimapa na tela (sem tocar no jogo).

O minimapa fica sempre centrado no jogador e com o norte para cima. Este módulo compara a captura do
minimapa com uma imagem de referência do mapa (a mesma imagem da rota) e devolve a posição do jogador
em pixels da referência. Usado pelo ChestTracker para a rota acompanhar o personagem.

    python seguir_mapa.py --ref rota.png --mini captura_do_minimapa.png     # testa com uma imagem salva
"""
import argparse
import ctypes
import logging
import sys
import threading
import time
from collections import OrderedDict

import cv2
import numpy as np

log = logging.getLogger("baus.mapa")

ESCALAS = tuple(round(0.5 + 0.1 * i, 1) for i in range(41))  # 0.5 a 4.5 px da referência por px do minimapa
ACORDO_PX = 12       # pedaços que apontam para o mesmo lugar (em px da referência) contam como acordo
MIN_ACORDO = 3       # de 5 pedaços (2 concordando acontece por acaso em qualquer imagem)
NOTA_GRUPO = 0.28    # nota média mínima do grupo que concordou
MIN_TEXTURA = 12.0   # desvio-padrão mínimo de um pedaço: área lisa (tela preta, parede) não vota
CONFIRMAR = 3        # leituras seguidas e coerentes antes de acreditar na posição
BUSCA_ESCALA_S = 10.0  # intervalo mínimo entre duas procuras de escala (sem escala nenhuma ainda)
BUSCA_ESCALA_CONHECIDA_S = 60.0  # escala que já achou o jogador nesta sessão: procurar outra é raro
BUSCA_ESCALA_DESISTINDO_S = 120.0  # várias procuras sem nunca achar: espaça para não pesar à toa
FALHAS_ESCALA = (20, 40)  # leituras seguidas sem achar antes de procurar escala: (ainda não deu certo, já deu)
# sem posição, a busca no mapa inteiro (a parte cara) espaça: 1 s no começo, depois 3 s e 5 s
PERDIDO_S = ((10.0, 1.0), (60.0, 3.0), (float("inf"), 5.0))  # (perdido há até N s, intervalo)
CACHE_ESCALAS = 3    # mapas reduzidos guardados (cada um tem vários MB)
SALTO_MAX = 90       # px da referência que o jogador pode andar entre duas leituras
LADO_TRABALHO = 128  # o minimapa é reduzido para este lado antes de comparar
FOLGA_PERTO = 24     # busca rápida: quantos px do minimapa o jogador pode ter andado desde a última leitura
MIN_NOTA = 0.2       # correlação mínima para um pedaço votar


def pedacos(lado):
    """Recortes do minimapa usados na comparação: 4 quadrantes e o miolo, como (x0, y0, x1, y1).

    Ficam de fora a borda, a faixa de baixo (nome do lugar) e o centro (seta do jogador).
    """
    m, baixo, meio, c = round(lado * 0.08), round(lado * 0.18), round(lado * 0.035), lado // 2
    return (("NO", (m, m, c - meio, c - meio)), ("NE", (c + meio, m, lado - m, c - meio)),
            ("SO", (m, c + meio, c - meio, lado - baixo)), ("SE", (c + meio, c + meio, lado - m, lado - baixo)),
            ("todo", (round(lado * 0.1), round(lado * 0.1), lado - round(lado * 0.1), lado - baixo)))


class Referencia:
    """Imagem do mapa (BGR) com as versões reduzidas por escala guardadas em cache."""

    def __init__(self, bgr):
        if bgr is None or bgr.ndim != 3:
            raise ValueError("imagem de referência inválida")
        self.bgr = bgr
        self._cache = OrderedDict()

    @classmethod
    def de_arquivo(cls, caminho):
        dados = np.fromfile(str(caminho), dtype=np.uint8)  # imread não abre caminho com acento no Windows
        return cls(cv2.imdecode(dados, cv2.IMREAD_COLOR))

    def reduzida(self, escala, guardar=True):
        """Mapa reduzido para a escala. `guardar=False` não ocupa o cache (procura de escala: uma vez cada)."""
        chave = round(escala, 3)
        if chave in self._cache:
            self._cache.move_to_end(chave)
            return self._cache[chave]
        img = cv2.resize(self.bgr, None, fx=1 / escala, fy=1 / escala, interpolation=cv2.INTER_AREA)
        if guardar:
            self._cache[chave] = img
            while len(self._cache) > CACHE_ESCALAS:  # memória limitada: sai a menos usada
                self._cache.popitem(last=False)
        return img


def localizar(mini, ref, escala, perto=None, guardar=True):
    """Posição do centro do minimapa na referência: (x, y, acordo, nota) ou None.

    Cada pedaço do minimapa procura o seu lugar na referência; só vale quando pelo menos MIN_ACORDO
    pedaços apontam para o mesmo ponto. Fora da área da referência eles discordam e o resultado é None.
    `perto=(x, y)` limita a busca à vizinhança da última posição (bem mais rápido).
    """
    lado = min(mini.shape[:2])
    mini = mini[:lado, :lado]
    if lado > LADO_TRABALHO * 1.5:  # compara em tamanho menor: 4x mais rápido, erro de 1-2 px
        fator = LADO_TRABALHO / lado
        mini = cv2.resize(mini, (LADO_TRABALHO, LADO_TRABALHO), interpolation=cv2.INTER_AREA)
        escala, lado = escala / fator, LADO_TRABALHO
    alvo = ref.reduzida(escala, guardar)
    c = lado // 2
    ox = oy = 0
    if perto is not None:
        raio = c + FOLGA_PERTO
        ox, oy = max(0, round(perto[0] / escala) - raio), max(0, round(perto[1] / escala) - raio)
        alvo = alvo[oy:round(perto[1] / escala) + raio, ox:round(perto[0] / escala) + raio]
    votos = []
    for _nome, (x0, y0, x1, y1) in pedacos(lado):
        t = mini[y0:y1, x0:x1]
        if t.shape[0] < 12 or t.shape[1] < 12 or t.shape[0] >= alvo.shape[0] or t.shape[1] >= alvo.shape[1]:
            continue
        if float(t.std()) < MIN_TEXTURA:
            continue
        _, nota, _, (px, py) = cv2.minMaxLoc(cv2.matchTemplate(alvo, t, cv2.TM_CCOEFF_NORMED))
        if np.isfinite(nota) and MIN_NOTA <= nota <= 1.0:
            votos.append(((px + ox + c - x0) * escala, (py + oy + c - y0) * escala, nota))
    melhor = None
    for vx, vy, _ in votos:
        grupo = [(x, y, n) for x, y, n in votos if abs(x - vx) <= ACORDO_PX and abs(y - vy) <= ACORDO_PX]
        if melhor is None or (len(grupo), sum(n for *_, n in grupo)) > (len(melhor), sum(n for *_, n in melhor)):
            melhor = grupo
    if not melhor:
        return None
    peso = sum(n for *_, n in melhor)
    if len(melhor) < MIN_ACORDO or peso / len(melhor) < NOTA_GRUPO:
        return None
    return (sum(x * n for x, _, n in melhor) / peso, sum(y * n for _, y, n in melhor) / peso,
            len(melhor), peso / len(melhor))


def achar_escala(mini, ref, escalas=ESCALAS):
    """Testa as escalas e devolve (escala, resultado) da que teve mais acordo, ou None.

    Como são muitas tentativas, aqui a exigência é maior: 4 dos 5 pedaços.
    """
    melhor = None
    for e in escalas:
        r = localizar(mini, ref, e, guardar=False)
        if r is not None and r[2] >= 4 and (melhor is None or (r[2], r[3]) > (melhor[1][2], melhor[1][3])):
            melhor = (e, r)
    return melhor


class Rastreador:
    """Thread que captura o minimapa e mantém a última posição conhecida do jogador.

    Uma leitura sozinha não vale: a posição só é publicada depois de CONFIRMAR leituras seguidas
    que caibam no quanto o jogador consegue andar. `ativo()` diz se vale a pena ler agora
    (jogo em primeiro plano e em partida); fora disso a posição é apagada.
    """

    def __init__(self, ref, regiao, escala=0.0, hz=5.0, capturar=None, ativo=None, escala_fixa=None,
                 relogio=time.monotonic, ao_achar_escala=None, confirmada=False):
        self.ref, self.regiao, self.escala, self.hz = ref, regiao, escala, hz  # regiao = (x, y, lado) em pixels
        self.capturar, self.ativo = capturar, ativo or (lambda: True)
        # escala fixa nunca é trocada; senão ela é procurada de novo de tempos em tempos enquanto nada bate
        self.escala_fixa = bool(escala) if escala_fixa is None else escala_fixa
        self.relogio, self._prox_busca, self._buscas = relogio, 0.0, 0
        self.medir = None         # medir(tipo, ms): para o modo --medir do contador
        self._perdido_desde = None
        self._confirmada = bool(confirmada and escala)  # a escala atual já achou o jogador (nesta ou noutra sessão)
        self.ao_achar_escala = ao_achar_escala  # chamado com a escala nova (para guardar no config)
        self._lock = threading.Lock()
        self._pos = None          # (x, y, escala, quando) já confirmada
        self._cand = None         # (x, y) da última leitura boa, ainda em confirmação
        self._seguidas = 0
        self._parar = threading.Event()
        self._thread = None
        self.falhas = 0

    def posicao(self, validade=1.5):
        """(x, y, escala) na referência se a última leitura confirmada tem menos de `validade` s; senão None."""
        with self._lock:
            p = self._pos
        if p is None or time.monotonic() - p[3] > validade:
            return None
        return p[:3]

    def esquecer(self):
        with self._lock:
            self._pos = None
        self._cand, self._seguidas = None, 0

    def passo(self, mini):
        """Processa uma captura do minimapa (BGR). Devolve a leitura (confirmada ou não) ou None."""
        r = None
        if self.escala:
            if self._cand is not None:  # primeiro só em volta de onde estava
                t0 = time.perf_counter()
                r = localizar(mini, self.ref, self.escala, perto=self._cand)
                self._medir("perto", t0)
            if r is None:
                t0 = time.perf_counter()
                r = localizar(mini, self.ref, self.escala)
                self._medir("mapa inteiro", t0)
        if r is None and not self.escala_fixa and (not self.escala or self.falhas >= FALHAS_ESCALA[self._confirmada]) \
                and self.relogio() >= self._prox_busca:
            # a busca é cara: escala que já deu certo quase nunca muda; sem acertar nunca, espaça também
            self._prox_busca = self.relogio() + (BUSCA_ESCALA_CONHECIDA_S if self._confirmada else
                                                 BUSCA_ESCALA_S if self._buscas < 6 else BUSCA_ESCALA_DESISTINDO_S)
            # com um palpite, só as vizinhas dele; sem palpite (ou a cada 3ª, se nunca deu certo), todas
            self._buscas += 1
            faixa = (0.9, 1.1) if self._confirmada else (0.7, 1.4)  # confirmada: só ajuste fino (3 tamanhos)
            perto_de = ESCALAS if not self.escala or (not self._confirmada and self._buscas % 3 == 0) else \
                [e for e in ESCALAS if faixa[0] * self.escala <= e <= faixa[1] * self.escala]
            t0 = time.perf_counter()
            achado = achar_escala(mini, self.ref, perto_de)
            self._medir("escala", t0)
            if achado:
                if abs(achado[0] - self.escala) > 0.05:
                    log.info("Minimapa: escala %.1f encontrada", achado[0])
                    self._confirmada = False  # escala nova: ainda precisa provar
                    if self.ao_achar_escala is not None:
                        try:
                            self.ao_achar_escala(achado[0])
                        except Exception:  # não conseguir guardar a escala não pode parar o rastreio
                            log.exception("Minimapa: não deu para guardar a escala")
                self.escala, r = achado
        if r is None:
            self.falhas += 1
            self._seguidas = 0
            if self.falhas >= 3:
                self._cand = None
                if self._perdido_desde is None:
                    self._perdido_desde = self.relogio()
            return None
        self._perdido_desde = None
        coerente = self._cand is not None and abs(r[0] - self._cand[0]) <= SALTO_MAX \
            and abs(r[1] - self._cand[1]) <= SALTO_MAX
        self._seguidas = self._seguidas + 1 if coerente else 1
        self._cand, self.falhas = (r[0], r[1]), 0
        if self._seguidas >= CONFIRMAR:
            self._confirmada = True
            with self._lock:
                self._pos = (r[0], r[1], self.escala, time.monotonic())
        return r

    def _medir(self, tipo, t0):
        if self.medir is not None:
            self.medir(tipo, (time.perf_counter() - t0) * 1000)

    def espera_perdido(self):
        """Intervalo entre buscas no mapa inteiro: cresce quanto mais tempo sem achar o jogador."""
        perdido = 0.0 if self._perdido_desde is None else self.relogio() - self._perdido_desde
        return max(1 / self.hz, next(s for ate, s in PERDIDO_S if perdido <= ate))

    def _rodar(self):
        cv2.setNumThreads(1)  # um núcleo só: a busca demora um pouco mais, mas não disputa a CPU com o jogo
        if sys.platform == "win32":  # abaixo do normal: o jogo sempre passa na frente
            ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -1)
        try:
            capturar = self.capturar or _captura_mss(self.regiao)
        except Exception as e:  # mss ausente ou sem acesso à tela
            log.error("Minimapa: captura de tela indisponível (%s)", e)
            return
        log.info("Minimapa: lendo %dx%d em +%d+%d, %.0f vezes por segundo",
                 self.regiao[2], self.regiao[2], self.regiao[0], self.regiao[1], self.hz)
        estado = None
        while not self._parar.is_set():
            t0 = time.monotonic()
            novo = "parado"
            try:
                if self.ativo():
                    self.passo(capturar())
                    novo = "achou" if self.posicao() is not None else "procurando"
                else:
                    self.esquecer()
            except Exception:  # um quadro com erro não pode parar o rastreio
                log.exception("Minimapa: erro ao processar a captura")
            if novo != estado:
                estado = novo
                p = self.posicao()
                log.debug("Minimapa: %s", f"posição ({p[0]:.0f}, {p[1]:.0f})" if p else
                          {"parado": "parado (fora do jogo ou da partida)"}.get(novo, "fora da área da rota"))
            espera = 0.5 if novo == "parado" else 1 / self.hz if self._cand is not None else self.espera_perdido()
            self._parar.wait(max(0.02, espera - (time.monotonic() - t0)))

    def iniciar(self):
        self._thread = threading.Thread(target=self._rodar, name="minimapa", daemon=True)
        self._thread.start()

    def parar(self):
        self._parar.set()


def _captura_mss(regiao):
    """Função que captura a região (x, y, lado) e devolve BGR. O mss é criado na thread que usa."""
    import mss
    sct = mss.mss()
    area = {"left": int(regiao[0]), "top": int(regiao[1]), "width": int(regiao[2]), "height": int(regiao[2])}
    return lambda: np.ascontiguousarray(np.asarray(sct.grab(area))[:, :, :3])


def autoteste():
    rng = np.random.default_rng(7)
    # mundo falso: manchas de cor suaves, como um terreno; a referência é só um pedaço dele
    base = rng.integers(0, 255, (100, 200, 3), dtype=np.uint8)
    mundo = cv2.GaussianBlur(cv2.resize(base, (2000, 1000), interpolation=cv2.INTER_CUBIC), (0, 0), 3)
    X0, Y0 = 200, 200
    ref = Referencia(mundo[Y0:Y0 + 600, X0:X0 + 900].copy())

    def minimapa(x, y, escala, lado=250):
        """Minimapa do jogador em (x, y) da referência."""
        meio = lado * escala / 2
        corte = mundo[round(Y0 + y - meio):round(Y0 + y + meio), round(X0 + x - meio):round(X0 + x + meio)]
        mini = cv2.resize(corte, (lado, lado), interpolation=cv2.INTER_AREA)
        ruido = rng.normal(0, 6, mini.shape)
        mini = np.clip(mini.astype(np.float32) * 1.08 + 5 + ruido, 0, 255).astype(np.uint8)  # outro brilho
        cv2.drawMarker(mini, (lado // 2, lado // 2), (255, 255, 255), cv2.MARKER_TRIANGLE_UP, 14, 3)
        cv2.rectangle(mini, (0, lado - 30), (lado, lado), (0, 0, 0), -1)  # faixa do nome do lugar
        return mini

    r = localizar(minimapa(430, 310, 2.0), ref, 2.0)
    assert r is not None and abs(r[0] - 430) < 4 and abs(r[1] - 310) < 4 and r[2] >= 4, r
    r = localizar(minimapa(436, 305, 2.0), ref, 2.0, perto=(430, 310))      # busca rápida em volta
    assert r is not None and abs(r[0] - 436) < 4 and abs(r[1] - 305) < 4, r
    e, r = achar_escala(minimapa(500, 280, 1.7), ref)
    assert abs(e - 1.7) < 0.11 and abs(r[0] - 500) < 6 and abs(r[1] - 280) < 6, (e, r)
    # perto da borda direita da referência: o pedaço "NE"/"SE" cai fora, os outros ainda acertam
    r = localizar(minimapa(700, 300, 2.0), ref, 2.0)
    assert r is not None and abs(r[0] - 700) < 6 and abs(r[1] - 300) < 6, r
    liso = np.full((250, 250, 3), 40, np.uint8)                              # tela lisa não vota
    assert localizar(liso, ref, 2.0) is None and achar_escala(liso, ref) is None
    # lugar do mundo que não está na referência: sem acordo
    fora = minimapa(1400, 300, 2.0)
    assert localizar(fora, ref, 2.0) is None
    assert [n for n, _ in pedacos(250)] == ["NO", "NE", "SO", "SE", "todo"]
    outro = fora

    rast = Rastreador(ref, (0, 0, 250), escala=2.0)
    assert rast.posicao() is None
    assert rast.passo(minimapa(430, 310, 2.0)) is not None and rast.posicao() is None   # 1 leitura não basta
    rast.passo(minimapa(433, 311, 2.0))
    assert rast.posicao() is None
    rast.passo(minimapa(436, 312, 2.0))                                      # 3 seguidas e coerentes: vale
    x, y, esc = rast.posicao()
    assert abs(x - 436) < 5 and abs(y - 312) < 5 and esc == 2.0
    assert rast.passo(outro) is None and rast.falhas == 1 and rast.posicao() is not None  # ainda na validade
    assert rast.posicao(validade=0) is None
    rast.passo(minimapa(650, 320, 2.0))                                      # salto impossível: recomeça a contagem
    assert rast._seguidas == 1 and abs(rast.posicao()[0] - 436) < 5
    rast.esquecer()
    assert rast.posicao() is None and rast._cand is None
    auto = Rastreador(ref, (0, 0, 250))
    for i in range(3):
        auto.passo(minimapa(500 + i, 280, 1.7))
    assert abs(auto.escala - 1.7) < 0.11 and auto.posicao() is not None and not auto.escala_fixa
    agora = [0.0]
    errada = Rastreador(ref, (0, 0, 250), escala=3.4, escala_fixa=False, relogio=lambda: agora[0])
    for i in range(60):                                                      # escala errada: nada bate...
        agora[0] += 6
        errada.passo(minimapa(500, 280, 1.7))
    assert abs(errada.escala - 1.7) < 0.11                                   # ...e ele acha a certa sozinho
    guardadas = []
    avisa = Rastreador(ref, (0, 0, 250), ao_achar_escala=guardadas.append, relogio=lambda: agora[0])
    avisa.passo(minimapa(500, 280, 1.7))
    assert len(guardadas) == 1 and abs(guardadas[0] - 1.7) < 0.11
    presa = Rastreador(ref, (0, 0, 250), escala=3.4, relogio=lambda: agora[0])
    for i in range(60):
        agora[0] += 6
        presa.passo(minimapa(500, 280, 1.7))
    assert presa.escala == 3.4 and presa.escala_fixa                         # fixa de verdade não muda
    # memória limitada: o cache guarda só CACHE_ESCALAS mapas; a procura de escala não guarda nenhum
    leve = Referencia(ref.bgr)
    for e in (1.0, 1.5, 2.0, 2.5, 3.0):
        leve.reduzida(e)
    assert list(leve._cache) == [2.0, 2.5, 3.0]
    leve.reduzida(2.0); leve.reduzida(3.5)
    assert list(leve._cache) == [3.0, 2.0, 3.5]                              # a usada agora fica
    achar_escala(minimapa(500, 280, 1.7), leve)
    assert len(leve._cache) == 3
    # escala confirmada noutra sessão: começa confiando nela (sem procurar escala na ilha de espera)
    assert Rastreador(ref, (0, 0, 250), escala=2.0, confirmada=True)._confirmada
    assert not Rastreador(ref, (0, 0, 250), escala=0.0, confirmada=True)._confirmada
    agora[0] = 0.0
    confiante = Rastreador(ref, (0, 0, 250), escala=2.0, confirmada=True, relogio=lambda: agora[0])
    buscas_feitas = []
    confiante.medir = lambda tipo, ms: tipo == "escala" and buscas_feitas.append(agora[0])
    for i in range(30):  # 30 falhas: ainda não desconfia (precisa de 40)
        agora[0] += 1
        confiante.passo(outro)
    assert buscas_feitas == []
    # perdido há muito tempo: a busca cara espaça (1 s, depois 3 s, depois 5 s)
    agora[0] = 1000.0
    longe = Rastreador(ref, (0, 0, 250), escala=2.0, hz=4, relogio=lambda: agora[0])
    assert longe.espera_perdido() == 1.0
    for _ in range(3):
        longe.passo(outro)
    agora[0] += 30; assert longe.espera_perdido() == 3.0
    agora[0] += 60; assert longe.espera_perdido() == 5.0
    for i in range(3):
        longe.passo(minimapa(430 + i, 310, 2.0))
    assert longe._perdido_desde is None and longe.espera_perdido() == 1.0  # achou: volta ao normal
    medidas = []
    longe.medir = lambda tipo, ms: medidas.append(tipo)
    longe.passo(minimapa(433, 310, 2.0))
    assert medidas == ["perto"]
    chamadas = []
    desligado = Rastreador(ref, (0, 0, 250), 2.0, hz=20, capturar=lambda: chamadas.append(1) or outro,
                           ativo=lambda: False)
    desligado.iniciar(); time.sleep(0.2); desligado.parar()
    assert chamadas == []                                                    # fora do jogo nem captura
    print("autoteste do minimapa ok")


def main():
    p = argparse.ArgumentParser(description="Localiza o jogador no mapa a partir de uma captura do minimapa")
    p.add_argument("--ref", help="imagem de referência do mapa (a da rota)")
    p.add_argument("--mini", help="captura do minimapa (quadrada)")
    p.add_argument("--escala", type=float, default=0.0, help="px da referência por px do minimapa (0 = procurar)")
    p.add_argument("--autoteste", action="store_true")
    a = p.parse_args()
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if a.autoteste:
        autoteste()
        return
    if not a.ref or not a.mini:
        p.error("informe --ref e --mini (ou --autoteste)")
    ref = Referencia.de_arquivo(a.ref)
    mini = cv2.imdecode(np.fromfile(a.mini, dtype=np.uint8), cv2.IMREAD_COLOR)
    if mini is None:
        p.error(f"não deu para abrir {a.mini}")
    if a.escala:
        escala, r = a.escala, localizar(mini, ref, a.escala)
    else:
        achado = achar_escala(mini, ref)
        escala, r = achado if achado else (0.0, None)
    if r is None:
        print("Não achei: o minimapa não bate com nenhuma parte da referência.")
    else:
        print(f"Jogador em x={r[0]:.0f} y={r[1]:.0f} na referência | escala {escala:.1f} | "
              f"{r[2]}/5 pedaços de acordo | nota {r[3]:.2f}")


if __name__ == "__main__":
    main()
