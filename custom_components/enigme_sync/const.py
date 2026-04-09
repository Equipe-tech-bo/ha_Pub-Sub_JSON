DOMAIN = "enigme_sync"

TOPIC_ACTION_SUFFIX  = "ACTION"
SYNC_PAYLOAD_PREFIX  = "sync"

DEFAULT_MQTT_FILTER  = "BR/#"
DEFAULT_JSON_PATH    = "/config/www/log/session.json"

# Suffixes de topics à ignorer (blacklist réception)
DEFAULT_TOPIC_BLACKLIST = ["ACTION", "RESET"]

# Profondeurs où envoyer l'ACTION (ex: 3 = SALLE/PIECE/ENIGME)
DEFAULT_ACTION_DEPTHS = [3]

# Intervalle du ping en secondes
DEFAULT_PING_INTERVAL = 30

# Payload du ping
PING_PAYLOAD = "PING"

PING_TIMEOUT = 5  # secondes
PING_ENTITY_PREFIX = "input_boolean."
PING_ENTITY_SUFFIX = "_ping"
