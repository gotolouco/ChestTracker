<p align="center">
  <img src="docs/banner.png" alt="ChestTracker — contador de baús para Fortnite" width="100%">
</p>

<p align="center">
  <a href="https://github.com/gotolouco/ChestTracker/releases/latest"><img src="https://img.shields.io/github/v/release/gotolouco/ChestTracker?label=vers%C3%A3o&color=FFD84D&labelColor=0A1640" alt="Versão"></a>
  <a href="https://github.com/gotolouco/ChestTracker/releases"><img src="https://img.shields.io/github/downloads/gotolouco/ChestTracker/total?label=downloads&color=3FD0FF&labelColor=0A1640" alt="Downloads"></a>
  <img src="https://img.shields.io/badge/Windows-10%20%7C%2011-2456D6?logo=windows&logoColor=white&labelColor=0A1640" alt="Windows 10 e 11">
  <img src="https://img.shields.io/badge/python-3.9%2B-2456D6?logo=python&logoColor=white&labelColor=0A1640" alt="Python 3.9+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/licen%C3%A7a-MIT-2456D6?labelColor=0A1640" alt="Licença MIT"></a>
</p>

<p align="center">
  <b>Overlay que conta os baús que você abre no Fortnite.</b><br>
  Segure <b>E</b> até o baú abrir e ele soma sozinho, num painel no estilo do HUD do jogo.
</p>

<p align="center">
  <a href="https://github.com/gotolouco/ChestTracker/releases/latest/download/ChestTracker.exe"><b>⬇ Baixar ChestTracker.exe</b></a>
</p>

<p align="center">
  <img src="docs/demo.gif" alt="O anel enche enquanto segura E, fica verde no tempo certo e o baú entra com +1" width="486">
</p>

## Recursos

- **Contagem automática:** segurar E pelo tempo de abrir um baú soma +1. Toque rápido (pegar item) e hold longo (reviver) não contam.
- **Anel de interação:** enche enquanto você segura E e fica verde quando o tempo já vai contar.
- **Sessão e ritmo:** mostra quantos baús você abriu desde que abriu o programa e quantos por hora.
- **Lobby, fila e partida:** lê o log do Fortnite e mostra no painel se você está no lobby, procurando partida ou em partida. Segurar E só conta dentro da partida, e o painel mostra os baús da partida atual e quantas partidas você jogou.
- **Chance do token pirata:** uma barra mostra a chance de já ter caído pelo menos um token da Loja Pirata, pelo total de baús contados.
- **Atalhos do seu jeito:** escolha qualquer tecla ou botão do mouse (meio, 4 e 5) para cada ação, na janela que abre antes do painel.
- **Não atrapalha o jogo:** o mouse passa através do painel, e a contagem fica salva em `baus.json`.

## Download

Baixe o **`ChestTracker.exe`** na [última versão](https://github.com/gotolouco/ChestTracker/releases/latest). Não precisa instalar Python.

1. Coloque o `.exe` numa pasta só dele. A contagem é salva em `baus.json` nessa pasta.
2. Deixe o Fortnite em **Tela cheia em janela**. Na tela cheia normal o overlay não aparece.
3. Abra o `ChestTracker.exe`, confira os atalhos e clique em **INICIAR**. Para fechar o painel, use o atalho de fechar (padrão **F10**).

> Enquanto o `.exe` não tiver assinatura digital (veja a [política de assinatura](#política-de-assinatura-de-código)), o Windows pode avisar:
> - **"O Windows protegeu o computador":** clique em **Mais informações → Executar assim mesmo**.
> - **"O controle inteligente de aplicativos bloqueou..."** (Windows 11): esse bloqueio não tem botão para continuar. Use a versão pelo código (abaixo) ou desative o Controle Inteligente de Aplicativos em **Segurança do Windows → Controle de aplicativos e navegador**.

### Rodando pelo código

1. Instale o [Python](https://www.python.org/downloads/) marcando **Add to PATH**.
2. Dê dois cliques em `iniciar.bat`. Ele instala o `pynput` e o `Pillow` e abre o overlay.

## Configuração

Antes do painel abre uma janela para ajustar tudo. As escolhas ficam salvas em `config.json`, ao lado do `.exe`, e voltam preenchidas na próxima vez.

<p align="center"><img src="docs/configuracao.png" alt="Janela de configuração com os atalhos e os ajustes" width="420"></p>

- **Atalhos:** clique num botão e aperte a tecla ou o botão do mouse. Cada ação aceita até 2 atalhos. **Esc** cancela e **Backspace** deixa o espaço vazio. Se o atalho já estava em outra ação, ele sai de lá.
- **Tempo mínimo e máximo:** segurar a tecla de abrir baú por um tempo entre os dois conta +1. O padrão é de 0,85 s a 3 s.
- **Tamanho e posição do painel:** o painel já acompanha a escala do Windows (125%, 150%) e fica nítido nela. Aumente se quiser ele maior.
- **Token pirata: 1 em:** a chance de um baú dar o token. O padrão é 1 em 137.500.
- **Mudar os números:** clique em **+** e **−** (segure para ir mais rápido) ou use a rodinha do mouse em cima do ajuste.

| Ação | Atalho padrão |
|---|---|
| Abrir baú (segurar) | E |
| Somar +1 | F8 ou Mouse 4 |
| Descontar −1 | F7 |
| Zerar total | F9 |
| Fechar contador | F10 |

O rodapé do painel mostra quanto tempo você segurou no último hold. Use esse valor para ajustar o tempo mínimo.

### Opções de linha de comando

| Opção | O que faz |
|---|---|
| `--direto` | pula a janela e abre o painel com o `config.json` salvo |
| `--arquivo` | onde salvar a contagem (padrão `baus.json`) |
| `--config` | onde salvar os atalhos e ajustes (padrão `config.json`) |
| `--zerar` | começa a contagem do 0 |
| `--debug` | log detalhado |
| `--autoteste` | roda os testes e sai |

<p align="center"><img src="docs/previa.png" alt="Estados do painel: parado, segurando E, pronto para contar e baú contado" width="400"></p>

## Limites

- Caixas de munição também abrem segurando E e contam como baú. Corrija com o atalho de descontar (padrão F7).
- O contador só lê o teclado, o mouse e o arquivo de log que o Fortnite grava em `%LOCALAPPDATA%\FortniteGame\Saved\Logs`. Ele não mexe no jogo.
- Se o log não for encontrado, o painel mostra **JOGO NÃO DETECTADO** e o E conta em qualquer lugar, como antes. Os atalhos de somar e descontar funcionam em qualquer fase.

## Nova versão

Mude o número no arquivo `VERSION` (ex.: `1.1.0`) e envie para a `main`. O GitHub testa, gera o `ChestTracker.exe` e publica a release `v1.1.0` sozinho.

## Política de assinatura de código

> **Em análise:** o pedido de assinatura gratuita à SignPath Foundation está sendo avaliado. Até a aprovação, o `.exe` é publicado sem assinatura.

Free code signing provided by [SignPath.io](https://about.signpath.io), certificate by [SignPath Foundation](https://signpath.org).

| Papel | Quem |
|---|---|
| Autores e revisores | [gotolouco](https://github.com/gotolouco) |
| Aprovadores (aprovam cada versão antes de assinar) | [gotolouco](https://github.com/gotolouco) |

O `ChestTracker.exe` é gerado só a partir do código deste repositório, pelo GitHub Actions ([release.yml](.github/workflows/release.yml)). Cada versão é aprovada manualmente antes de ser assinada.

### Privacidade

Este programa não envia nenhuma informação para outros computadores ou serviços, a menos que o usuário peça. Ele lê o teclado e o mouse só para reagir aos atalhos configurados, lê o log local do Fortnite só para saber a fase do jogo, e guarda apenas a contagem (`baus.json`) e os atalhos (`config.json`) na própria pasta.

*This program will not transfer any information to other networked systems unless specifically requested by the user or the person installing or operating it.*

## Licença

[MIT](LICENSE) © 2026 gotolouco

A fonte [Anton](assets/Anton-Regular.ttf), de Vernon Adams, é distribuída sob a [SIL Open Font License 1.1](assets/Anton-OFL.txt).

---

<sub>ChestTracker é um projeto de fã e não tem ligação com a Epic Games. Fortnite é marca registrada da Epic Games, Inc.</sub>
