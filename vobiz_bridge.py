"""
vobiz_bridge.py — WebSocket bridge between Vobiz Voice Application and LiveKit room.
Handles live bidirectional streaming so Vobiz calls can be processed natively by agent.py.
"""

import asyncio
import base64
import json
import logging
import os
import uuid
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect
from livekit import api, rtc

logger = logging.getLogger("vobiz-bridge")


async def handle_vobiz_websocket(websocket: WebSocket):
    await websocket.accept()
    logger.info("Vobiz WebSocket connected!")

    room: Optional[rtc.Room] = None
    source: Optional[rtc.AudioSource] = None
    stream_id: Optional[str] = None
    call_id: Optional[str] = None
    caller_phone: str = "unknown"
    agent_task: Optional[asyncio.Task] = None

    livekit_url = os.getenv("LIVEKIT_URL", "")
    api_key = os.getenv("LIVEKIT_API_KEY", "")
    api_secret = os.getenv("LIVEKIT_API_SECRET", "")

    async def stream_agent_audio(remote_track: rtc.RemoteAudioTrack):
        try:
            audio_stream = rtc.AudioStream(remote_track)
            logger.info("Started streaming agent audio to Vobiz")
            async for frame in audio_stream:
                if not stream_id:
                    continue
                payload = base64.b64encode(frame.data).decode("utf-8")
                msg = {
                    "event": "playAudio",
                    "streamId": stream_id,
                    "media": {
                        "contentType": "audio/x-l16",
                        "sampleRate": frame.sample_rate,
                        "payload": payload,
                    },
                }
                await websocket.send_text(json.dumps(msg))
        except Exception as exc:
            logger.warning(f"Agent audio stream ended: {exc}")

    try:
        while True:
            text = await websocket.receive_text()
            data = json.loads(text)
            event = data.get("event")

            if event == "start":
                start_info = data.get("start", {})
                stream_id = start_info.get("streamId")
                call_id = start_info.get("callId", str(uuid.uuid4())[:8])
                caller_phone = start_info.get("from") or start_info.get("caller") or "inbound_caller"
                logger.info(f"Vobiz stream started — streamId={stream_id} callId={call_id} from={caller_phone}")

                room_name = f"inbound-{call_id}"

                # 1. Create room and trigger LiveKit agent dispatch
                lk_api = api.LiveKitAPI(livekit_url, api_key, api_secret)
                try:
                    await lk_api.room.create_room(
                        api.CreateRoomRequest(
                            name=room_name,
                            empty_timeout=30,
                            agents=[
                                api.RoomAgentDispatch(
                                    agent_name="outbound-caller",
                                    metadata=json.dumps({"inbound": True, "phone_number": caller_phone}),
                                )
                            ],
                        )
                    )
                    logger.info(f"Created LiveKit room {room_name} with agent dispatch")
                except Exception as cre:
                    logger.warning(f"Room create note: {cre}")
                finally:
                    await lk_api.aclose()

                # 2. Generate participant token
                token = (
                    api.AccessToken(api_key, api_secret)
                    .with_identity(f"sip_{caller_phone}")
                    .with_name(caller_phone)
                    .with_grants(api.VideoGrants(room_join=True, room=room_name))
                    .to_jwt()
                )

                # 3. Connect as caller participant
                room = rtc.Room()

                @room.on("track_subscribed")
                def on_track_subscribed(track, publication, participant):
                    if track.kind == rtc.TrackKind.KIND_AUDIO:
                        logger.info(f"Agent audio track detected from {participant.identity}")
                        nonlocal agent_task
                        agent_task = asyncio.create_task(stream_agent_audio(track))

                await room.connect(livekit_url, token)
                logger.info(f"Connected to LiveKit room {room_name}")

                # 4. Create and publish audio track for caller
                source = rtc.AudioSource(16000, 1)
                local_track = rtc.LocalAudioTrack.create_audio_track("caller_mic", source)
                await room.local_participant.publish_track(local_track)
                logger.info("Published caller microphone track to LiveKit")

            elif event == "media":
                media_info = data.get("media", {})
                payload_b64 = media_info.get("payload")
                if payload_b64 and source:
                    pcm_data = base64.b64decode(payload_b64)
                    samples = len(pcm_data) // 2
                    frame = rtc.AudioFrame(
                        data=pcm_data,
                        sample_rate=16000,
                        num_channels=1,
                        samples_per_channel=samples,
                    )
                    await source.capture_frame(frame)

            elif event == "stop":
                logger.info(f"Vobiz stream stopped for {call_id}")
                break

    except WebSocketDisconnect:
        logger.info("Vobiz WebSocket disconnected")
    except Exception as exc:
        logger.error(f"Vobiz WebSocket error: {exc}", exc_info=True)
    finally:
        if agent_task and not agent_task.done():
            agent_task.cancel()
        if room:
            try:
                await room.disconnect()
            except Exception:
                pass
        logger.info("Vobiz bridge session cleaned up")
