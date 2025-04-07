import json
import yaml
from loguru import logger
import sys
import asyncio
import json
from time import time
from os import environ
import traceback
import websockets
import pickle

from requests import get


in_addon = not __file__.startswith("/workspaces/") and not __file__.startswith(
    "/home/chris/projects/"
)

try:
    with open("./data/options.json") as json_file:
        settings = json.load(json_file)
except FileNotFoundError:
    with open("./spotify-tracker/config.yaml") as yaml_file:
        full_config = yaml.safe_load(yaml_file)

    settings = full_config["options"]

try:
    with open("./spotify-tracker/test_config.yaml") as yaml_file:
        test_config = yaml.safe_load(yaml_file)
        settings.update(test_config)
except FileNotFoundError:
    test_config = {}

# reset the logging level from DEBUG by default
logger.remove()
logger.add(sys.stderr, level=settings["logging_level"] if in_addon else "DEBUG")

base_url = "http://supervisor/core/api" if in_addon else test_config.get("base_url")

logger.debug(f"Base URL: {base_url}")

if in_addon:
    token = environ.get("SUPERVISOR_TOKEN")
else:
    token = test_config.get("token")

tx_id = 1

tz = "America/Chicago"


def get_new_tx_id():
    global tx_id
    if tx_id >= 65535:
        tx_id = 1
    else:
        tx_id += 1
    return tx_id


def time_as_int():
    return int(time())


class Tracker:

    def __init__(
        self, media_player_id: str, skip_enabled_id: str, block_hours: float = 24
    ):

        self.media_player_id = media_player_id
        self.skip_enabled_id = skip_enabled_id
        self.skip_enabled = None
        self.block_hours = block_hours

        try:
            with open(f"./data/{self.media_player_id}.pickle", "rb") as handle:
                self.track_dict = pickle.load(handle)
                logger.info(
                    f"Loaded {self.media_player_id} track dict from pickle storage - {len(self.track_dict)} items"
                )
        except FileNotFoundError:
            self.track_dict = {}
            logger.info(f"Created new track dict for {self.media_player_id}")

    def should_skip_track(self, media_id) -> bool:

        if self.skip_enabled is None:
            self.get_skip_enabled_status()

        track_time = self.track_dict.get(media_id)

        # the track has not been played
        if track_time is None:
            self.track_dict[media_id] = time_as_int()
            return False

        # it's been long enough that we can replay the track
        if time_as_int() - track_time > self.block_hours * 3600:
            # update the dict with the new timestamp
            self.track_dict[media_id] = time_as_int()
            return False

        # it hasn't been long enough to replay track
        # should skip here, but only if skip enabled
        return self.skip_enabled is True

    def get_skip_enabled_status(self):
        get_value_url = f"{base_url}/states/{self.skip_enabled_id}"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        logger.info(f"Getting skip enabled status for {self.skip_enabled_id}")

        get_r = get(url=get_value_url, headers=headers)

        if get_r.ok:
            r_json = get_r.json()
            self.skip_enabled = r_json["state"] == "on"

    def cleanup_tracks(self):

        for media_id, track_time in self.track_dict.copy().items():
            if time_as_int() - track_time > self.block_hours * 3600:
                try:
                    logger.info(
                        f"Cleaning up track in {self.media_player_id}: {media_id}"
                    )
                    self.track_dict.pop(media_id)
                except Exception as e:
                    logger.warning(
                        f"Exception cleaning up track in {self.media_player_id}: {media_id}: {type(e)} -- {e}"
                    )

        with open(f"./data/{self.media_player_id}.pickle", "wb") as handle:
            pickle.dump(self.track_dict, handle, protocol=pickle.HIGHEST_PROTOCOL)
            logger.info(
                f"Saved {self.media_player_id} track dict to pickle storage - {len(self.track_dict)} items"
            )


async def spotify_monitor():

    logger.info("Starting Spotify Monitor")
    logger.info(f"Base URL: {base_url}")
    logger.info(f'Block hours: {settings["block_for_x_hours"]}')

    logger.info(f"Spotify media players: {settings['spotify_media_players']}")
    logger.info(f"Skip enabled helpers: {settings['skip_enabled_helpers']}")
    logger.info(f"Logging level: {settings['logging_level']}")

    cleanup_tracks_interval_secs = 300
    spotify_player_ids = settings["spotify_media_players"].split(",")
    skip_enabled_ids = settings["skip_enabled_helpers"].split(",")

    player_dict = {
        x: Tracker(x, skip_enabled_ids[i], settings["block_for_x_hours"])
        for i, x in enumerate(spotify_player_ids)
    }

    while True:

        try:

            async with websockets.connect(
                base_url.replace("http://", "ws://") + "/websocket"
            ) as ha_ws:

                connected_to_ws = False
                # connect and receive the first message
                connect_response = await ha_ws.recv()

                auth_response = None
                if json.loads(connect_response)["type"] == "auth_required":

                    # Send authentication message
                    auth_data = json.dumps({"type": "auth", "access_token": token})
                    await ha_ws.send(auth_data)

                    # Receive response (hopefully successful authentication)
                    auth_response = await ha_ws.recv()
                    json_auth_response = json.loads(auth_response)

                    if json_auth_response["type"] == "auth_ok":
                        connected_to_ws = True
                        logger.info("Connected to Home Assistant Websocket API")

                if not connected_to_ws:
                    logger.critical(f"Unable to authenticate to HA: {auth_response}")
                    raise ConnectionError(
                        f"Unabled to authenticate to HA: {auth_response}"
                    )

                for media_player_id in spotify_player_ids:

                    msg = {
                        "id": get_new_tx_id(),
                        "type": "subscribe_trigger",
                        "trigger": {
                            "platform": "state",
                            "entity_id": media_player_id,
                            "attribute": "media_content_id",
                        },
                    }

                    await ha_ws.send(json.dumps(msg))
                    sub_resp = await ha_ws.recv()
                    sub_resp_data = json.loads(sub_resp)
                    if not sub_resp_data["success"]:
                        logger.critical(
                            f"Unable to subscribe to {media_player_id} triggers: {sub_resp}"
                        )
                        raise ConnectionError(
                            f"Unable to subscribe to {media_player_id} triggers: {sub_resp}"
                        )

                for skip_enabled_id in skip_enabled_ids:

                    msg = {
                        "id": get_new_tx_id(),
                        "type": "subscribe_trigger",
                        "trigger": {
                            "platform": "state",
                            "entity_id": skip_enabled_id,
                        },
                    }

                    await ha_ws.send(json.dumps(msg))
                    sub_resp = await ha_ws.recv()
                    sub_resp_data = json.loads(sub_resp)
                    if not sub_resp_data["success"]:
                        logger.critical(
                            f"Unable to subscribe to {skip_enabled_id}: {sub_resp}"
                        )
                        raise ConnectionError(
                            f"Unable to subscribe to {skip_enabled_id}: {sub_resp}"
                        )

                st = time_as_int() - 999999
                while ha_ws.open:

                    msg = await ha_ws.recv()
                    msg_json = json.loads(msg)

                    try:
                        if msg_json["type"] == "result":
                            continue
                    except Exception:
                        pass

                    try:
                        entity_id = msg_json["event"]["variables"]["trigger"][
                            "to_state"
                        ]["entity_id"]

                        if entity_id in skip_enabled_ids:
                            skip_enabled_on = (
                                msg_json["event"]["variables"]["trigger"]["to_state"][
                                    "state"
                                ]
                                == "on"
                            )

                            for p in player_dict.values():
                                if p.skip_enabled_id == entity_id:
                                    p.skip_enabled = skip_enabled_on
                                    logger.info(
                                        f"Skip enabled changed to {skip_enabled_on} for {p.media_player_id}"
                                    )
                                    break

                            continue

                        to_attributes = msg_json["event"]["variables"]["trigger"][
                            "to_state"
                        ]["attributes"]
                        media_content_id = to_attributes["media_content_id"]

                        skip_track = player_dict[media_player_id].should_skip_track(
                            media_content_id
                        )

                        if skip_track:
                            logger.info(
                                f"Skipping - {entity_id} - {to_attributes['media_artist']} - {to_attributes['media_title']}"
                            )

                            skip_msg = {
                                "id": get_new_tx_id(),
                                "type": "call_service",
                                "domain": "media_player",
                                "service": "media_next_track",
                                "target": {"entity_id": entity_id},
                            }
                            await ha_ws.send(json.dumps(skip_msg))
                        else:
                            logger.info(
                                f"Playing - {entity_id} - {to_attributes['media_artist']} - {to_attributes['media_title']}"
                            )

                    except Exception as e:
                        logger.warning(
                            f"Exception processing message: {type(e)} -- {e}"
                        )
                        continue

                    if st + cleanup_tracks_interval_secs < time_as_int():
                        logger.info(
                            f"Cleanup interval of {cleanup_tracks_interval_secs:,} seconds passed, running cleanup"
                        )
                        for p in player_dict.values():
                            p.cleanup_tracks()

                        st = time_as_int()

        except Exception as e:
            logger.critical(traceback.print_exc())
            logger.critical(f"Exception: {type(e)}: {e}")
            await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(spotify_monitor())
