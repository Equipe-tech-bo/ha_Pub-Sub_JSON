# ha_Pub-Sub_JSON

Intégration Home Assistant pour stocker les états d'énigmes dans un JSON
et les resynchroniser via MQTT.

## MQTT 

- Écoute : `+/+/+/ACTION`
- Stocke dans `config/www/log/session.json`

## Service

`enigme_sync.sync` — Republier depuis le JSON vers MQTT.

| Param | Exemple | Description |
|-------|---------|-------------|
| path  | `BR.CRYPTE` | Lieu, Lieu.Salle, ou Lieu.Salle.Enigme |

## Installation

HACS → Dépôts personnalisés → URL du repo → Intégration
