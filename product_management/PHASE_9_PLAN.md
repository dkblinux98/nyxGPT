# Phase 9 — Interact with the agents on Zoom (voice/video)

**Created:** 2026-07-15
**Owner decision (2026-07-15):** give the agents a **Zoom** presence — the owner wants to talk
to the nyxGPT agents in real time on a call, not just in text. Split out from the Slack
interactive work (Phase 7/8) because voice/video is a distinct, harder integration.

## Goal

Join a Zoom meeting with one (or more) of the nyxGPT agents and converse by voice: ask the
scrummaster for status, tell the developer to pick up an issue, review a PR out loud, etc. The
agent hears you, thinks/acts through the same runtime that powers Slack and the pipeline, and
speaks back.

## Approach — a meeting bot on the shared agent runtime

Zoom is another **frontend on the Phase 7 daemon**, not a separate agent. The added layer is a
real-time voice bridge:

1. **Join the call** — a bot participant via the **Zoom Meeting SDK** (or Zoom RTMS / real-time
   media streams) that can receive meeting audio and send audio back.
2. **Speech-to-text** — stream meeting audio through STT (e.g. Whisper-class, local for the
   privacy posture) to get the user's utterances.
3. **Agent runtime** — route the transcript to the same OpenClaw-style runtime that backs Slack
   and the pipeline (Phase 7), with turn/session context for the call.
4. **Text-to-speech** — synthesize the agent's reply and play it back into the meeting.
5. **Turn-taking** — barge-in/interruption handling, wake addressing ("hey scrummaster…"),
   and knowing which agent is being addressed.

## Depends on

- **Phase 7** — the persistent daemon that hosts the agents (Zoom is a frontend on it).
- Ideally after the **Slack** interactive path is proven (same runtime, simpler transport),
  so Phase 9 only adds the voice bridge, not the agent plumbing.

## Considerations

- **Latency** — real-time voice needs a tight STT→agent→TTS loop; streaming at each stage.
- **Privacy/local-first** — prefer local STT/TTS where feasible; be explicit about any meeting
  audio leaving the machine (VISION.md: no silent exfiltration). Handle recording consent.
- **Addressing** — which agent answers; multi-agent on one call vs a single "voice of nyxGPT."
- **Auth/identity** — the bot authenticates as an agent identity; meeting-join credentials and
  Zoom app scopes are owner-managed secrets.

## Status: reserved — scope refined when Phase 7 (daemon) and the Slack path land.
