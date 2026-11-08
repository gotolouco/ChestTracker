#!/usr/bin/env node
/*
 * Conta os baús que você abriu e os prêmios da Caça ao Tesouro lendo os replays do Fortnite.
 * Licença: GPL-3.0-or-later (o .exe leva junto o ooz-wasm, que é GPL). Veja replays/LICENSE.
 *
 *   node ler_replays.js               lê os replays novos e mostra o total
 *   node ler_replays.js --refazer     relê todos (ignora o que já está em replays.json)
 *   node ler_replays.js --teste       testa a contagem com dados montados
 *
 * "Seu" boneco = o PlayerPawn que tem Controller: só o dono do replay recebe o próprio Controller.
 * Baú seu = baú que virou bAlreadySearched enquanto o SearchingPawn era um dos seus bonecos.
 * Item seu = pickup que terminou bPickedUp com PickupTarget num dos seus bonecos.
 * Elemental novo = a mochila do seu boneco troca de Elemental (o primeiro é o que você já tinha equipado).
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const assert = require('assert');

const PASTA_DEMOS = path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'),
  'FortniteGame', 'Saved', 'Demos');
const SAIDA = path.join(__dirname, '..', 'replays.json');
const VERSAO = 5; // mude quando a contagem mudar: o cache antigo é relido

// Containers vasculháveis que o parser não conhece por padrão (ele já tem Tiered_Chest_Athena e Tiered_Ammo_Athena).
// shortcut: lista do mapa Delilah (Fortnite 43.00); temporada nova com baú novo -> rode com --debug numa pasta
// vazia e procure "SearchingPawn" em notReadingGroups.txt
const OUTROS_CONTAINERS = [
  '/Game/Athena/Items/ForagedItems/BuriedChest/B_Athena_BuriedChest_Chest.B_Athena_BuriedChest_Chest_C',
  '/Game/Building/ActorBlueprints/Containers/AlwaysSpawn_NormalChest.AlwaysSpawn_NormalChest_C',
  '/Game/Building/ActorBlueprints/Containers/AlwaysSpawn_RareChest.AlwaysSpawn_RareChest_C',
  '/Game/Building/ActorBlueprints/Containers/Tiered_Chest_5_Child.Tiered_Chest_5_Child_C',
  '/RelicChest/Gameplay/B_TieredChest_Relics.B_TieredChest_Relics_C',
  '/SpritePosters/Blueprints/Items/Booty/B_SpritePosters_Booty_Chest.B_SpritePosters_Booty_Chest_C',
  '/SpritePosters/Gameplay/RelicChests/B_Delilah_TieredChest_Relics_Variant1.B_Delilah_TieredChest_Relics_Variant1_C',
  '/SpritePosters/Gameplay/RelicChests/B_Delilah_TieredChest_Relics_Variant2.B_Delilah_TieredChest_Relics_Variant2_C',
  // não são baús: entram em "outros" (caixa de comida, barril de vara de pesca)
  '/Game/Building/ActorBlueprints/Containers/FoodBox_Produce_Athena.FoodBox_Produce_Athena_C',
  '/Game/Building/ActorBlueprints/Containers/Barrel_FishingRod_Container_Athena.Barrel_FishingRod_Container_Athena_C',
];

// Mochila que mostra o Elemental que o jogador carrega (some ao trocar de Elemental na partida)
const MOCHILA = {
  path: ['/SpriteExtractionGameplay/Gameplay/Backpack/BP_ExtractionBackpack_Sprite.BP_ExtractionBackpack_Sprite_C'],
  customExportName: 'Mochila',
  parseLevel: 1,
  exportGroup: 'mapData',
  exportName: 'mochilas',
  exportType: 'array',
  properties: {
    OwningPawn: { name: 'OwningPawn', type: 'uint', parseFunction: 'readIntPacked', parseType: 'default' },
    ExtractableItemDefinition: { name: 'ExtractableItemDefinition', type: 'ItemDefinition', parseType: 'readClass' },
  },
};

// Baú animado da Caça ao Tesouro: aparece quando alguém abre um baú e ganha um prêmio
const BAU_PREMIO = {
  path: ['/DelilahTreasureHuntGameplay/Gameplay/Actors/BGA_DelilahTreasureHuntAnimatedChest.BGA_DelilahTreasureHuntAnimatedChest_C'],
  customExportName: 'BauPremio',
  parseLevel: 1,
  exportGroup: 'mapData',
  exportName: 'premios',
  exportType: 'array',
  properties: {
    OpeningPawn: { name: 'OpeningPawn', type: 'uint', parseFunction: 'readIntPacked', parseType: 'default' },
    bSuccess: { name: 'bSuccess', type: 'boolean', parseFunction: 'readBit', parseType: 'default' },
    // struct com o id em texto ("DelilahPrize:dpid_item15"): lido cru e o id sai por regex
    PrizeTemplateId: { name: 'PrizeTemplateId', type: 'DebugObject', parseType: 'readClass' },
  },
};

// Nome no jogo de cada prêmio. shortcut: só os ids já confirmados; id novo aparece cru até entrar aqui
const PREMIOS = {};

function nomePremio(id) {
  return PREMIOS[id] || id.replace(/^DelilahPrize:dpid_/, 'prêmio ');
}

const AJUDA = `Conta os baús que você abriu e os prêmios da Caça ao Tesouro lendo os replays do Fortnite.

  --pasta <pasta>   onde estão os replays (padrão: ${PASTA_DEMOS})
  --saida <arquivo> onde salvar o resultado (padrão: replays.json)
  --refazer         relê todos os replays
  --debug           mostra os erros do leitor de replays
  --teste           roda os testes e sai`;

function args(argv) {
  const a = { pasta: PASTA_DEMOS, saida: SAIDA, refazer: false, debug: false, teste: false };
  for (let i = 0; i < argv.length; i += 1) {
    const k = argv[i];
    if (k === '--pasta') a.pasta = argv[++i];
    else if (k === '--saida') a.saida = argv[++i];
    else if (k === '--refazer') a.refazer = true;
    else if (k === '--debug') a.debug = true;
    else if (k === '--teste') a.teste = true;
    else if (k === '-h' || k === '--help') a.ajuda = true;
    else throw new Error(`opção desconhecida: ${k} (use --help)`);
  }
  if (!a.ajuda && !a.teste && (!a.pasta || !a.saida)) throw new Error('--pasta e --saida precisam de um caminho');
  return a;
}

function classe(pathName) {
  return String(pathName || '').split('.').pop().replace(/_C$/, '');
}

// Caça ao Tesouro: todo baú de Battle Royale sorteia esses prêmios (chance por baú, divulgada pela Epic).
// As semanas acabam domingo 11:00 de Brasília (14:00 UTC), para todo mundo; o nome do replay é a hora local de quem jogou.
// shortcut: usa a chance normal; nas Treasure Hours (sábado) o carro e o traje ficam até 10x mais fáceis
const TESOURO = [
  {
    nome: 'Semana 1 (A$AP Arrives)', fim: new Date(Date.UTC(2026, 10, 8, 14, 0)),
    premios: [['Solid Gold Rugahand (traje)', 1562500], ['Golden Llama Crew (carro)', 7813],
      ['Solid Gold Gauntlets (picareta)', 568], ['NO TRESPASSING (faixa)', 391], ['Plunder in Paradise (tela)', 272]],
  },
  {
    nome: 'Semana 2', fim: new Date(Date.UTC(2026, 10, 15, 14, 0)),
    premios: [['Solid Gold Lil Uzi Vert (traje)', 1562500], ['Golden Llama Crew (carro)', 7813],
      ['Diamond and Gold (mochila)', 568], ['London (faixa)', 391], ['So Kawaii (tela)', 272]],
  },
];
const TESOURO_SEMPRE = [['Pirate Shop Access', 137500], ['Dispositivo (qualquer)', 127]];

/** "UnsavedReplay-2026.11.08-17.32.49.replay" -> Date local da partida */
function dataReplay(nome) {
  const m = /(\d{4})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})/.exec(nome);
  return m ? new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6]) : null;
}

/** Chance de ter achado pelo menos 1 em n baús, com chance 1 em `um` por baú */
function chance(n, um) {
  return 1 - (1 - 1 / um) ** n;
}

function pct(x) {
  return `${(x * 100).toLocaleString('pt-BR', { maximumFractionDigits: x < 0.01 ? 2 : 1 })}%`;
}

/** Baús por semana da Caça ao Tesouro, a partir de { nomeDoReplay: { baus } } */
function bausPorSemana(replays) {
  const n = TESOURO.map(() => 0);
  for (const [nome, r] of Object.entries(replays)) {
    const d = dataReplay(nome);
    const s = d && TESOURO.findIndex((w) => d < w.fim);
    if (s >= 0 && !r.erro) n[s] += r.baus;
  }
  return n;
}

/** Tudo o que a janela TESOUROS do ChestTracker mostra; vai em replays.json -> "resumo" */
function resumir_tesouro(replays) {
  const n = bausPorSemana(replays);
  const lidos = Object.values(replays);
  const todos = n.reduce((x, y) => x + y, 0);
  const premio = (baus) => ([nome, um]) => ({ nome, um, chance: chance(baus, um) });
  const achados = [];
  for (const [replay, r] of Object.entries(replays)) {
    for (const id of Object.keys(r.tesouros || {})) achados.push({ id, nome: nomePremio(id), replay });
  }
  return {
    atualizado: new Date().toISOString(),
    partidas: lidos.filter((r) => !r.erro).length,
    erros: lidos.filter((r) => r.erro).length,
    baus: lidos.reduce((x, r) => x + (r.baus || 0), 0),
    semanas: TESOURO.map((w, i) => ({ nome: w.nome, fim: w.fim.toISOString(), baus: n[i], premios: w.premios.map(premio(n[i])) })),
    sempre: TESOURO_SEMPRE.map(premio(todos)),
    dispositivosEsperados: todos / 127,
    achados,
  };
}

function textoTesouro(resumo) {
  const linhas = [`\nTotal em ${resumo.partidas} partidas: ${resumo.baus} baús`];
  linhas.push(`Tesouros que você achou: ${resumo.achados.map((a) => `${a.nome} (${a.replay})`).join(', ') || 'nenhum'}`);
  linhas.push('\nCaça ao Tesouro (chance de já ter achado, pelos baús que você abriu):');
  for (const w of resumo.semanas) {
    linhas.push(`  ${w.nome}, até ${new Date(w.fim).toLocaleString('pt-BR', { dateStyle: 'short', timeStyle: 'short' })}: ${w.baus} baús`);
    for (const p of w.premios) linhas.push(`    ${p.nome}: 1 em ${p.um.toLocaleString('pt-BR')} -> ${pct(p.chance)}`);
  }
  for (const p of resumo.sempre) linhas.push(`  ${p.nome}: 1 em ${p.um.toLocaleString('pt-BR')} -> ${pct(p.chance)}`);
  linhas.push(`  Dispositivos esperados: ${resumo.dispositivosEsperados.toLocaleString('pt-BR', { maximumFractionDigits: 1 })}`);
  return linhas.join('\n');
}

function categoria(item) {
  if (/^AthenaAmmoData/.test(item)) return 'munição';
  if (/^(Wood|Stone|Metal)ItemData/.test(item)) return 'materiais';
  if (/^WID_/.test(item)) return 'armas e consumíveis';
  return 'outros';
}

/** Junta o que o parser viu num resumo da partida. Função pura: é o que o --teste confere. */
function resumir(chests, pickups, meusPawns, mochilas = [], premios = []) {
  const meus = new Set(meusPawns);
  const r = { baus: 0, municao: 0, outros: 0, porTipo: {}, itens: 0, porCategoria: {}, porItem: {}, elementais: {}, tesouros: {} };
  for (const p of premios) if (p.ok && meus.has(p.pawn)) r.tesouros[p.id] = (r.tesouros[p.id] || 0) + 1;
  const vistos = new Set();
  for (const m of mochilas.filter((x) => meus.has(x.pawn)).sort((x, y) => x.t - y.t)) {
    // o 1º é o equipado; ao renascer ele volta, por isso cada Elemental conta uma vez por partida
    // shortcut: pegar de novo um Elemental que já carregou na partida não conta
    if (vistos.size && !vistos.has(m.def)) r.elementais[m.def] = (r.elementais[m.def] || 0) + 1;
    vistos.add(m.def);
  }
  for (const c of chests) {
    if (!c.bAlreadySearched || !meus.has(c.SearchingPawn)) continue;
    const tipo = classe(c.pathName);
    r.porTipo[tipo] = (r.porTipo[tipo] || 0) + 1;
    if (/Ammo/.test(tipo)) r.municao += 1;
    else if (/Chest/.test(tipo)) r.baus += 1;
    else r.outros += 1;
  }
  for (const p of pickups) {
    if (!meus.has(p.alvo)) continue;
    r.itens += 1;
    const cat = categoria(p.item);
    r.porCategoria[cat] = (r.porCategoria[cat] || 0) + 1;
    r.porItem[p.item] = (r.porItem[p.item] || 0) + 1;
  }
  return r;
}

async function lerReplay(arquivo, debug) {
  const parse = require('./fortnite-replay-parser');
  const padrao = require('./fortnite-replay-parser/export/handleEventEmitter');
  const baseBaus = require('./fortnite-replay-parser/NetFieldExports/Chests.json');

  const meusPawns = new Set();
  const pickups = [];          // um por pickup que alguém terminou de pegar
  const abertos = new Map();   // chIndex -> estado do pickup naquele canal
  const baus = new Map();      // id do baú -> { pathName, bAlreadySearched, SearchingPawn de quem abriu }
  const mochilas = [];         // { t, pawn, def }: cada vez que a mochila de Elemental de alguém muda
  const premios = new Map();   // ator do baú animado -> { pawn, ok, id }
  const donoMochila = new Map(); // ator da mochila -> boneco dono

  const config = {
    parseLevel: 10,
    // um export por classe: o parser liga cada id de ator fixo a um grupo só
    customNetFieldExports: [...OUTROS_CONTAINERS.map((p) => ({ ...baseBaus, path: [p], staticActorIds: [classe(p)] })), MOCHILA, BAU_PREMIO],
    handleEventEmitter: (em) => {
      padrao(em);
      em.propertyExportEmitter.on('PlayerPawn_Athena.PlayerPawn_Athena_C', ({ chIndex, data, changedProperties, globalData }) => {
        if (changedProperties.includes('Controller') && data.Controller) meusPawns.add(globalData.channelToActor[chIndex]);
      });
      // SearchingPawn volta a 0 depois de aberto: guarda quem estava vasculhando quando abriu
      em.propertyExportEmitter.on('Chests', ({ chIndex, staticActorId, data, changedProperties, globalData }) => {
        const id = staticActorId || `ator${globalData.channelToActor[chIndex]}`; // canal é reaproveitado, o ator não
        const b = baus.get(id) || { pathName: data.pathName, bAlreadySearched: false, SearchingPawn: 0, vasculhando: 0 };
        baus.set(id, b);
        if (changedProperties.includes('SearchingPawn') && data.SearchingPawn) b.vasculhando = data.SearchingPawn;
        if (changedProperties.includes('bAlreadySearched') && data.bAlreadySearched && !b.bAlreadySearched) {
          b.bAlreadySearched = true;
          b.SearchingPawn = b.vasculhando;
        }
      });
      em.propertyExportEmitter.on('FortniteGame.FortPickupAthena', ({ chIndex, data, changedProperties }) => {
        const p = abertos.get(chIndex) || {};
        abertos.set(chIndex, p);
        for (const k of ['ItemDefinition', 'PickupTarget', 'bPickedUp']) if (changedProperties.includes(k)) p[k] = data[k];
        if (p.bPickedUp && !p.contado && p.PickupTarget) {
          p.contado = true;
          pickups.push({ item: (p.ItemDefinition && p.ItemDefinition.name) || '?', alvo: p.PickupTarget });
        }
      });
      // o canal é reaproveitado por outro ator depois de fechar
      em.propertyExportEmitter.on('Mochila', ({ chIndex, data, changedProperties, globalData, timeSeconds }) => {
        const ator = globalData.channelToActor[chIndex];
        if (changedProperties.includes('OwningPawn') && data.OwningPawn) donoMochila.set(ator, data.OwningPawn);
        const def = data.ExtractableItemDefinition && data.ExtractableItemDefinition.name;
        if (changedProperties.includes('ExtractableItemDefinition') && def && donoMochila.has(ator)) {
          mochilas.push({ t: timeSeconds, pawn: donoMochila.get(ator), def });
        }
      });
      em.propertyExportEmitter.on('BauPremio', ({ chIndex, data, changedProperties, globalData }) => {
        const ator = globalData.channelToActor[chIndex];
        const p = premios.get(ator) || { pawn: 0, ok: false, id: '' };
        premios.set(ator, p);
        if (changedProperties.includes('OpeningPawn')) p.pawn = data.OpeningPawn;
        if (changedProperties.includes('bSuccess')) p.ok = data.bSuccess;
        if (changedProperties.includes('PrizeTemplateId') && data.PrizeTemplateId && data.PrizeTemplateId.data) {
          p.id = (/DelilahPrize:\w+/.exec(Buffer.from(data.PrizeTemplateId.data).toString('latin1')) || [''])[0];
        }
      });
      em.parsingEmitter.on('channelClosed', ({ chIndex }) => abertos.delete(chIndex));
    },
  };

  const log = console.log;
  if (!debug) console.log = () => {}; // o parser imprime cada pacote ruim
  try {
    await parse(fs.readFileSync(arquivo), config); // a contagem vem dos eventos acima
    return { ...resumir([...baus.values()], pickups, [...meusPawns], mochilas, [...premios.values()]), meusPawns: meusPawns.size };
  } finally {
    console.log = log;
  }
}

function carregar(saida) {
  try {
    const d = JSON.parse(fs.readFileSync(saida, 'utf8'));
    return d.versao === VERSAO && d.replays ? d : { versao: VERSAO, replays: {} };
  } catch (e) {
    if (e.code !== 'ENOENT') console.warn(`${saida} inválido (${e.message}), começando do zero`);
    return { versao: VERSAO, replays: {} };
  }
}

function salvar(saida, dados) {
  const tmp = `${saida}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(dados, null, 2));
  fs.renameSync(tmp, saida); // grava inteiro ou nada: não corrompe o replays.json se fechar no meio
}

async function main() {
  const a = args(process.argv.slice(2));
  if (a.ajuda) {
    console.log(AJUDA);
    return;
  }
  if (a.teste) return teste();

  if (!fs.existsSync(a.pasta)) throw new Error(`pasta de replays não encontrada: ${a.pasta}`);
  try {
    require('./fortnite-replay-parser');
  } catch (e) {
    // sem isso cada replay "falha" e o erro fica guardado como se o replay estivesse estragado
    throw new Error(`faltam as dependências do leitor de replays (${e.message.split('\n')[0]}). `
      + 'Rode "npm install" na pasta replays ou dê dois cliques no contar_replays.bat');
  }
  const dados = carregar(a.saida);
  const arquivos = fs.readdirSync(a.pasta).filter((f) => f.toLowerCase().endsWith('.replay')).sort();
  const novos = arquivos.filter((f) => {
    const st = fs.statSync(path.join(a.pasta, f));
    const c = dados.replays[f];
    // replay com erro é tentado de novo: falha rápido, e o erro pode ter sido do leitor e não do arquivo
    return a.refazer || !c || c.erro || c.tamanho !== st.size || c.mtime !== st.mtimeMs;
  });
  console.log(`${arquivos.length} replays em ${a.pasta}, ${novos.length} para ler`);

  for (let i = 0; i < novos.length; i += 1) {
    const f = novos[i];
    const arq = path.join(a.pasta, f);
    const st = fs.statSync(arq);
    const t0 = Date.now();
    process.stdout.write(`[${i + 1}/${novos.length}] ${f} ... `);
    try {
      const r = await lerReplay(arq, a.debug);
      dados.replays[f] = { tamanho: st.size, mtime: st.mtimeMs, ...r };
      const aviso = r.meusPawns ? '' : '  (não achei seu boneco: replay de espectador?)';
      const achou = Object.keys(r.tesouros).map(nomePremio).join(', ');
      console.log(`${r.baus} baús${achou ? `, TESOURO: ${achou}` : ''} (${((Date.now() - t0) / 1000).toFixed(1)} s)${aviso}`);
    } catch (e) {
      // replay cortado (jogo fechou) ou de versão que o parser não lê. Se o arquivo mudar
      // (partida ainda gravando), tamanho/mtime mudam e ele é relido na próxima vez
      dados.replays[f] = { tamanho: st.size, mtime: st.mtimeMs, erro: e.message };
      console.log(`falhou: ${e.message}`);
      if (a.debug) console.error(e.stack);
    }
    salvar(a.saida, dados);
  }

  // replays apagados pelo Fortnite continuam somando: o cache é o histórico
  dados.resumo = resumir_tesouro(dados.replays);
  salvar(a.saida, dados);
  if (dados.resumo.erros) console.log(`\n${dados.resumo.erros} replays não puderam ser lidos (cortados ou de outra versão): ficam fora da conta`);
  console.log(textoTesouro(dados.resumo));
  console.log(`Detalhes por partida em ${a.saida}`);
}

function teste() {
  const chests = [
    { pathName: '/Game/x/Tiered_Chest_Athena.Tiered_Chest_Athena_C', bAlreadySearched: true, SearchingPawn: 10 },
    { pathName: '/Game/x/Tiered_Ammo_Athena.Tiered_Ammo_Athena_C', bAlreadySearched: true, SearchingPawn: 10 },
    { pathName: '/Game/x/AlwaysSpawn_RareChest.AlwaysSpawn_RareChest_C', bAlreadySearched: true, SearchingPawn: 20 },
    { pathName: '/Game/x/B_Athena_BuriedChest_Chest.B_Athena_BuriedChest_Chest_C', bAlreadySearched: true, SearchingPawn: 20 },
    { pathName: '/Game/x/FoodBox_Produce_Athena.FoodBox_Produce_Athena_C', bAlreadySearched: true, SearchingPawn: 10 },
    { pathName: '/Game/x/Tiered_Chest_Athena.Tiered_Chest_Athena_C', bAlreadySearched: true, SearchingPawn: 99 },  // outro jogador
    { pathName: '/Game/x/Tiered_Chest_Athena.Tiered_Chest_Athena_C', bAlreadySearched: false, SearchingPawn: 10 }, // largou no meio
  ];
  const pickups = [
    { item: 'WID_Assault_Flintlock_Athena_UC', alvo: 10 },
    { item: 'AthenaAmmoDataBulletsMedium', alvo: 20 },
    { item: 'WoodItemData', alvo: 10 },
    { item: 'WID_Assault_Flintlock_Athena_UC', alvo: 99 },
  ];
  const r = resumir(chests, pickups, [10, 20]); // 20 = renasceu com outro boneco
  assert.deepStrictEqual([r.baus, r.municao, r.outros, r.itens], [3, 1, 1, 3]);
  assert.deepStrictEqual(r.porTipo,
    { Tiered_Chest_Athena: 1, Tiered_Ammo_Athena: 1, AlwaysSpawn_RareChest: 1, B_Athena_BuriedChest_Chest: 1, FoodBox_Produce_Athena: 1 });
  assert.deepStrictEqual(r.porCategoria, { 'armas e consumíveis': 1, 'munição': 1, materiais: 1 });
  assert.strictEqual(resumir(chests, pickups, []).baus, 0);
  assert.throws(() => args(['--xyz']));
  assert.strictEqual(dataReplay('UnsavedReplay-2026.11.08-17.32.49.replay').getTime(), new Date(2026, 10, 8, 17, 32, 49).getTime());
  assert.deepStrictEqual(bausPorSemana({
    'UnsavedReplay-2026.11.07-20.00.00.replay': { baus: 5 },  // sábado: semana 1 (em qualquer fuso)
    'UnsavedReplay-2026.11.09-20.00.00.replay': { baus: 7 },  // segunda: semana 2
    'UnsavedReplay-2026.11.10-20.00.00.replay': { erro: 'x' }, // replay com erro não soma
    'UnsavedReplay-2026.11.20-20.00.00.replay': { baus: 9 },  // depois da última semana: fora
  }), [5, 7]);
  assert.strictEqual(chance(0, 7813), 0);
  assert.ok(Math.abs(chance(7813, 7813) - 0.632) < 0.001); // 1 - 1/e
  assert.strictEqual(pct(0.2226), '22,3%');
  const premios = [
    { pawn: 11042, ok: true, id: 'DelilahPrize:dpid_item15' }, // seu
    { pawn: 10428, ok: true, id: 'DelilahPrize:dpid_item18' }, // outro jogador
    { pawn: 11042, ok: false, id: 'DelilahPrize:dpid_item17' }, // não ganhou
  ];
  assert.deepStrictEqual(resumir([], [], [11042], [], premios).tesouros, { 'DelilahPrize:dpid_item15': 1 });
  assert.strictEqual(nomePremio('DelilahPrize:dpid_item15'), 'prêmio item15');
  const res = resumir_tesouro({
    'UnsavedReplay-2026.11.09-16.35.49.replay': { baus: 13, tesouros: { 'DelilahPrize:dpid_item15': 1 } },
    'UnsavedReplay-2026.11.07-16.35.49.replay': { baus: 7 },
    'UnsavedReplay-2026.11.08-05.41.00.replay': { erro: 'cortado' },
  });
  assert.deepStrictEqual([res.partidas, res.erros, res.baus, res.semanas[0].baus, res.semanas[1].baus], [2, 1, 20, 7, 13]);
  assert.deepStrictEqual(res.achados, [{ id: 'DelilahPrize:dpid_item15', nome: 'prêmio item15', replay: 'UnsavedReplay-2026.11.09-16.35.49.replay' }]);
  assert.strictEqual(res.semanas[1].premios[1].um, 7813);
  // mochila: o equipado (CoinPile) não conta, renascer com o mesmo não conta, a troca conta; outro jogador não conta
  const mochilas = [
    { t: 2, pawn: 12, def: 'ESD_CoinPileSprite_Variant_Jeweled' },
    { t: 89, pawn: 15686, def: 'ESD_CoinPileSprite_Variant_Jeweled' },
    { t: 614, pawn: 28572, def: 'ESD_TreasureChestSprite_Variant_Jeweled' },
    { t: 700, pawn: 99, def: 'ESD_ParrotSprite_Variant_A' },
    { t: 800, pawn: 28572, def: 'ESD_CoinPileSprite_Variant_Jeweled' }, // renasceu: equipado volta, não conta
  ];
  const e = resumir([], [], [12, 15686, 28572], mochilas);
  assert.deepStrictEqual(e.elementais, { ESD_TreasureChestSprite_Variant_Jeweled: 1 });
  console.log('teste ok');
}

main().catch((e) => {
  console.error(`erro: ${e.message}`);
  process.exit(1);
});
