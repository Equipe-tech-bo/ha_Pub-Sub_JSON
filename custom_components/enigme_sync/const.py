DOMAIN = "enigme_sync"

TOPIC_ACTION_SUFFIX  = "ACTION"
SYNC_PAYLOAD_PREFIX  = "sync"

DEFAULT_MQTT_FILTER  = "BR/#"
DEFAULT_JSON_PATH    = "/config/www/log/session.json"

# Suffixes de topics à ignorer (blacklist réception)
DEFAULT_TOPIC_BLACKLIST = ["ACTION", "RESET"]

# Profondeurs où envoyer l'ACTION (ex: 3 = SALLE/PIECE/ENIGME)
DEFAULT_ACTION_DEPTHS = [3]
