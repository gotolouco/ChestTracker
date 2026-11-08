# Licença do contador de replays

O `ContadorReplays.exe` (gerado desta pasta pelo GitHub Actions) junta três partes:

| Parte | Licença |
|---|---|
| `ler_replays.js` | GPL-3.0-or-later |
| `fortnite-replay-parser/` (xNocken, com correções) | MIT, veja `fortnite-replay-parser/LICENSE` |
| `ooz-wasm` (descompactador Oodle, instalado pelo npm) | GPL-3.0-or-later |

Como o `ooz-wasm` é GPL, o contador de replays inteiro é distribuído sob a **GPL-3.0-or-later** (texto em `LICENSE-GPL-3.0.txt`). O código-fonte é esta pasta.

O ChestTracker (`contador_baus.py`) continua MIT. Ele só roda o `ContadorReplays.exe` como um programa separado e lê o `replays.json` que ele grava.
