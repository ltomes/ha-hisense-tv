"""Shared Media DAG attribute keys.

These constants define the contract between source integrations (nvidia_shield,
hisense_tv, etc.) and the media_zone aggregator. All source integrations MUST
use these exact attribute keys so media_zone can read them uniformly.
"""

# Role of this entity in the media DAG
ATTR_MEDIA_DAG_ROLE = "media_dag_role"

# Receiver this source is wired to (entity_id)
ATTR_MEDIA_DAG_RECEIVER_ENTITY = "media_dag_receiver_entity"

# Which receiver input this source occupies
ATTR_MEDIA_DAG_RECEIVER_INPUT = "media_dag_receiver_input"

# Jellyfin media_player entity for rich metadata
ATTR_MEDIA_DAG_JELLYFIN_ENTITY = "media_dag_jellyfin_entity"

# Now-playing dict: {state, title, media_type, series?, season?, episode?,
#                    duration?, position?, thumbnail?, artist?, album?, media_info?, source?}
ATTR_MEDIA_DAG_NOW_PLAYING = "media_dag_now_playing"

# List of running app dicts: [{package, state, title?, artist?}, ...]
ATTR_MEDIA_DAG_RUNNING_APPS = "media_dag_running_apps"
