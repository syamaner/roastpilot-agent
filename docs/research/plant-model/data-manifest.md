# Phase-1 plant-model study -- data manifest (fingerprint, not data)

This manifest is how the study's raw inputs are "committed" under the `AGENTS.md` no-roast-logs rule: the **fingerprint + provenance** are in the repo, the **bytes are not**. The raw `.alog` files, the SQLite DB, and raw per-tick telemetry live only at the documented local paths (and, in future, the Snowflake `roast_telemetry` table). Regenerate every study artifact with `scripts/plant_model_arx_study.py` against inputs that match these checksums.

## Artisan `.alog` corpus

- Source dir (local, not committed): `/Users/sertanyamaner/Library/Mobile Documents/com~apple~CloudDocs/roasting`
- Usable roasts (marked FC + drop, >= 120 s): **47**

| # | filename | sha256 | samples |
|---|---|---|---|
| 1 | `24-06-23_1237-australia-1.alog` | `fefdb911311de67ae962bbd9fbe3940e829c632979f9e7f3c28cfd3f652796d6` | 1021 |
| 2 | `24-06-23_1330-australia-2.alog` | `e7df500c6bbdf869ad30250400252e955b41bd9890a47469041c5266b75fcc0e` | 1159 |
| 3 | `24-09-07_1053-kona-1.alog` | `25cee9d57d6d61b0841ea1a8d5371235e054d0615caa20897c7736cd4cd5c4f3` | 885 |
| 4 | `24-09-07_1155-kona-2.alog` | `d95fa0656a6a4592feef0e2039bfb3dfd727eff8c82cfe2237664ee985afcda8` | 819 |
| 5 | `24-09-07_1243-taiwan.alog` | `c631abbf79cd18442914adda80c052584a1776544b3291a21d906bf8b5eb55d5` | 796 |
| 6 | `24-09-07_1326-au-nica.alog` | `ff4da90467c8f2248cdce7cc89d8b95a0ed2d26781273fd7e4ef5dea3bf30718` | 776 |
| 7 | `24-09-07_1410-au-so.alog` | `3ecc2d857e6b153c49d0be27b3a27cf238d10dc98c76b918fb2b218052499a8b` | 1424 |
| 8 | `24-09-07_1517-nicragua.alog` | `11e82f0f578a9c6a38b2250e3f431235147bf14b85918ae3a10ff1882a0c90c1` | 863 |
| 9 | `24-10-06_1456-alisan-1.alog` | `b05c45ca13545d4e170cb93875704362991d3a54406cb4fbb0a5dab33b398ec6` | 738 |
| 10 | `24-10-06_1546-alisan-2.alog` | `be3fa679b3d9de151f9182649cb4908aa445ee1a88c32c60f0b11556812172b0` | 934 |
| 11 | `24-10-06_1640-alisan-3.alog` | `0dd2cd0b6410b79b058f8fce933a4ed5e9ece72e0046dabf6ed874551a64bf78` | 753 |
| 12 | `25-07-16_1854_b1.alog` | `a10bb230c871a98bd7f45294004f00e43ecfb2ef08408a77e9dfb211f0da39d6` | 783 |
| 13 | `25-07-16_1933-cub-b2.alog` | `925159206677bd9f50c334f9ae3315022f9f8009434cf89f8a788f637c59c026` | 781 |
| 14 | `25-07-16_2009-b3.alog` | `0b9b6bdafce7998ff7311c7113c0af273bba872e38f71a5c0c1b568814b479b6` | 769 |
| 15 | `25-07-17_1845-cuba-b1.alog` | `b89328fa31edbcc5d37bbb1b97135b389332f2bf3957a711d3d718290501dd1d` | 893 |
| 16 | `25-07-17_1921-cuba-b2.alog` | `1ac6b1cc17d84d2dd08f379a94d485762f977f3d17e7c5abf65ebd560ca4bd33` | 769 |
| 17 | `25-10-12_1211-costarica-hermosa-2.alog` | `b598c94ee75c35f68b1ab7776b6c6039e9f2cbe6eee771782498b911aeb66cca` | 672 |
| 18 | `25-10-12_1211-costarica-hermosa-3.alog` | `5249d3b472475689ecafca0471048e4f7d365474d61272e4c548fd3e05b2c222` | 680 |
| 19 | `25-10-12_1211-costarica-hermosa-4.alog` | `533ca9a3894dd7a17583d5fc7bd6b32dc68a9c13f32e83bded2c601c21de8520` | 772 |
| 20 | `25-10-12_1211-costarica-hermosa.alog` | `b0c36a3b76314d238fc47ed86b52deae1dbc58b78c6f17280e7e9d92c3ed898d` | 753 |
| 21 | `25-10-19_1051-1-costarica.alog` | `812897b81c41efc857e65f0fc88b5535237349c3db4e7d4c9e5e8610769801ad` | 789 |
| 22 | `25-10-19_1126-brazil1.alog` | `71b7332bb03763f61d6455686e0d867a8617301c737532715abb4ee386a3d7a7` | 651 |
| 23 | `25-10-19_1154-brazil2.alog` | `604ca2a7c008c06b9f6438421b8c342a381d9f815e3ebcd5f267d910f3d2b459` | 658 |
| 24 | `25-10-19_1225-brazil3.alog` | `1ab14235dab2923db7c61f05a021e64e23c09acdce9aed1a540fd740321f4207` | 637 |
| 25 | `25-10-19_1305-brazil4.alog` | `fe3841d6f317e8f1f081635a6cbf662fba6ce824008da0b8fe1a4eebb8c5510d` | 694 |
| 26 | `brasil-ferm-b2-24-11-09_1656.alog` | `1d6755661b6bde29651f0cb451fe5b65dcad0d5eb66456149285c90baf441ab8` | 967 |
| 27 | `brasil-ferm-b3-24-11-09_1743.alog` | `a53847731bfafe3bbc3b8979d7fa2a25f837485efc8dae7dda304392f28d34da` | 812 |
| 28 | `brasil-fm-1-24-11-09_1610.alog` | `23bdc729052a83a5f8d455b7010d749a2e5fd5ba37c9e1522ce621095932450a` | 913 |
| 29 | `brasil_25-03-15_1310.alog` | `78277854348ba30a040d91485c3b6a3d5cdfb5a5076ca9ad84b1599dff1702a0` | 1046 |
| 30 | `cuba-1-25-03-30_1134.alog` | `403e83429867d5af07656348e633d8ff4d55e368cd70ec6da67e31a27961c324` | 1094 |
| 31 | `cuba-2-25-03-30_1214.alog` | `5e14ce45836b7b72cadcf7c90316cb20e5a257a8070eb4e5252ec48b7c010db7` | 837 |
| 32 | `cuba-3-25-03-30_1252.alog` | `2676b2c7416856f7c132b91d3461ca714b87eb95b3f7d1c5faf4a8020d8dcceb` | 1008 |
| 33 | `cuba_25-03-16_1235.alog` | `5ca6cc72cb2d672c1baea6e27abc0fbb769edb6c9f1af1d129e7e339c78823de` | 873 |
| 34 | `cuba_25-03-16_1318.alog` | `7114c895ce178c0d92a0320ebd7576283612e1755b2ac12821cb09a3bf1be9eb` | 889 |
| 35 | `ind2-25-04-06_1039.alog` | `17acbbd92d11c4ad71830378073c8eeb6b558a563a1232735ee498b39b0ab6f4` | 1057 |
| 36 | `indo-25-04-06_0951.alog` | `10647c358d15917c913c4a46f4e29564e9e068a5349e23ca3f655ca247ad5e05` | 1126 |
| 37 | `jamaica_4_24-12-06_2056.alog` | `3ed3a1438551afabb09a7f9979d83ee40377f834b25d74aabca65274849a8c03` | 1215 |
| 38 | `jamaica_b2_24-12-06_1938.alog` | `00ea39e70435ce64db01dad0f877a4e9133761dc99c20d4a70e33c777c42e4a2` | 857 |
| 39 | `jamaice_b1_24-12-06_1855.alog` | `d7dc23fda43ffe3b59fea57d86e06d6ba97099d3ec37f2ccf32c7cfaa750b977` | 1006 |
| 40 | `kona-24-11-12_1943.alog` | `481b3d9e42ea184aec7f7a670af3dda3376123474ea4fec7042d2d50f5605827` | 865 |
| 41 | `kona_3_dark24-12-06_2016.alog` | `3f51fa9aaacc0a909dfd665ff35b48d8a527c25e8a9802ae6e0d619dfc288410` | 877 |
| 42 | `nicaragua_25-03-15_1706.alog` | `3d7d5f126c17bc947ee257d93d4b5f150bae9e35d49384619c837ea582059735` | 1008 |
| 43 | `taiwan-nantou.alog` | `53b60894c57513f71f226f56c4c5cd3fa15c1580585aa6f83ee6402b3ca5aac7` | 973 |
| 44 | `tw_alisan_1.alog` | `04ab210f4dc1db1903caf5bd7523cf9cbb544d3f5cd21258f936c4fe2428e3fe` | 871 |
| 45 | `tw_nantouu-24-10-20_1503.alog` | `69493d10e1f3bc4d0900f964d9408609533afe6a501a4c7adeef0122d653d29d` | 845 |
| 46 | `vietnam-b1-24-10-27_1418.alog` | `cde82b87f01958322615a3fffe0dfb02b99b6499624a263267366faa58d0fa7f` | 985 |
| 47 | `vietnam-b2-24-10-27_1458.alog` | `7012763f347b1be8af053c401a98b097cef6000b23e9f7ce172c170bdb8bb7ec` | 955 |

## Store roasts (`roastpilot.sqlite3`)

- Source DB (local, not committed): `/Users/sertanyamaner/roasts/roastpilot.sqlite3`
- DB file sha256 at study time: `47b0e056a45ed2904aee731d3f276a14e61e222a7d2727a9afa7d7ba842530ba`
  (advisory only -- the DB grows as new roasts are recorded; the run ids below pin the exact rows.)
- Completed runs in DB: **13**
- Completed runs actually modelled (>= 60 usable telemetry rows): **13**

Completed-run telemetry is immutable once the run's completion trigger has fired, so the run id pins the data. Runs with fewer than 60 usable `roasting_pre_first_crack`/`development` telemetry rows are skipped by the harness.

| # | run_id | modelled |
|---|---|---|
| 1 | `3fbfd8882d144965b1a2de4de8721d87` | no |
| 2 | `5a32334c8da643eab8638032756a7cf7` | no |
| 3 | `d251013e220c4364bb0a122de6a93244` | no |
| 4 | `b74153ed91bf4e5d81715ca7a0c7ffec` | no |
| 5 | `f3fc65fa6e72422597f1acb5e62fe135` | no |
| 6 | `bf85c77a5436406285571a75df017512` | no |
| 7 | `a4299aea124b43d289bd425d4dc850c4` | no |
| 8 | `d55b0fce6c184e878042a6210d5c28f7` | no |
| 9 | `edbe9a76364342ed9b1338affd77c758` | no |
| 10 | `98fab734a83b4bab864c14f6a003040e` | no |
| 11 | `43c84c98f052485ab35a98264d7ff8b5` | no |
| 12 | `8ac8a5e4122941ca8109700fce92bc68` | no |
| 13 | `f24fca980468443884227a9ed1e55486` | no |

## Exclusion note

Per `AGENTS.md`, raw roast logs (`.alog`, SQLite DBs, raw per-tick telemetry, `step_response_traces.csv`) are intentionally **not** in the repo. Only code, the aggregate outputs (`loro_rmse.csv`, `landmarks.csv`, `model_summary.json`, the report), and this fingerprint are committed. The raw artifacts are fully regenerable from the harness against the checksummed inputs above.
