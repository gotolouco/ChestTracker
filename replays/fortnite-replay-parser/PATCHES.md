# fortnite-replay-parser 1.4.8 (xNocken, MIT) com correções

Cópia de https://github.com/xNocken/replay-reader (npm `fortnite-replay-parser@1.4.8`), sem docs/testes.
A versão do npm não lê os replays do Fortnite 43.00 (EngineNetworkVersion 45). Mudanças, todas marcadas com `// patch:`:

- `src/replayData/receivedPacket.js`: lê o bit `bPartialCustomExportsFinal` dos bunches parciais (versão 45+). Sem isso quase todo bunch grande sai desalinhado e os bonecos (PlayerPawn) nunca aparecem.
- `src/Classes/NetGuidCache.js`: ids de ator fixo agora são `Tiered_Chest_Athena_C_UAID_<hex>_<n>`; pega a classe antes do `_C_`. Sem isso nenhum baú aparece.
- `src/Classes/NetGuidCache.js`: `tryGetTagName` não quebra quando a tabela de gameplay tags não veio.
- `src/replayData/receiveNetFieldExportsCompat.js`: nome do método errado (`getNetFieldExportGroupFromIndex`).
- `src/replayData/parsePlaybackPackets.js`: um pacote com erro é pulado em vez de abortar o replay.
- `export/propertyExport/handlePlayerState.js`: a lista de espectadores era do módulo e vazava de um replay para o próximo na mesma execução; agora fica no `states` de cada replay.
