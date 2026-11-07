# ChestTracker

![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white) ![Windows](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D6?logo=windows&logoColor=white) [![Release](https://img.shields.io/github/v/release/gotolouco/ChestTracker?label=download)](https://github.com/gotolouco/ChestTracker/releases/latest)

Contador de baús do Fortnite: overlay em cima do jogo que conta os baús que você abre. Um baú conta quando você segura **E** pelo tempo de abertura (padrão: entre 0,85 s e 3 s).

![Prévia do overlay](docs/previa.png)

## Download

Baixe o **`ChestTracker.exe`** na [última versão](https://github.com/gotolouco/ChestTracker/releases/latest). Não precisa instalar Python.

1. Coloque o `.exe` numa pasta só dele. A contagem é salva em `baus.json` nessa pasta.
2. Deixe o Fortnite em **Tela cheia em janela**. Na tela cheia normal o overlay não aparece.
3. Abra o `ChestTracker.exe`. Para fechar, aperte **F10**.

> O Windows pode mostrar "O Windows protegeu o computador", porque o `.exe` não tem assinatura digital paga. Clique em **Mais informações → Executar assim mesmo**.

### Rodando pelo código

1. Instale o [Python](https://www.python.org/downloads/) marcando **Add to PATH**.
2. Dê dois cliques em `iniciar.bat`. Ele instala o `pynput` e abre o overlay.

## Atalhos

| Tecla | Ação |
|---|---|
| Segurar E | +1 baú, se o tempo estiver entre `--min` e `--max` |
| F8 ou Mouse 4 | +1 baú |
| F7 | −1 baú |
| F9 | zera o total |
| F10 | fecha |

## Opções

Funcionam no `.exe` e no script:

```
ChestTracker.exe --min 0.85 --max 3.0 --escala 1.5 --x 30 --y 30
```

| Opção | Padrão | O que faz |
|---|---|---|
| `--min` | 0.85 | segundos mínimos segurando E para contar |
| `--max` | 3.0 | acima disso não conta (ex.: reviver alguém) |
| `--tecla` | e | tecla de interação |
| `--escala` | 1.0 | tamanho do painel (1.5 em telas 2K/4K) |
| `--x`, `--y` | 30 | posição do painel na tela |
| `--arquivo` | baus.json | onde salvar a contagem |
| `--zerar` | — | começa do 0 |
| `--debug` | — | log detalhado |
| `--autoteste` | — | roda os testes da contagem e sai |

O rodapé mostra quanto tempo você segurou E no último hold. Use esse valor para ajustar o `--min`.

## Limites

- Caixas de munição também abrem segurando E e contam como baú. Corrija com F7.
- O contador só lê o teclado e o mouse e desenha uma janela. Ele não mexe no jogo.
