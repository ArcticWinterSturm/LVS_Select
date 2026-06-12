# LVS Selection Assist

LVS Selection Assist is an AI-assisted culling companion for FastStone Image Viewer and DigiKam. It is designed to sit quietly on top of your normal viewer workflow and make shoot sorting faster, clearer, and less repetitive.

Instead of bouncing between tools, you stay in the viewer, move through JPEG previews, and make quick decisions with the number keys. LVS keeps the overlay visible, helps you judge the frame, and bridges those choices back to the original RAW files so the selection process stays clean from start to finish.

## Highlights

LVS adds a live overlay that shows:
- AI scores
- captions generated from the image
- score tweaks informed by Florence-2 keywords
- a red / green / orange visual cue system that makes culling faster at a glance

That combination is done to give you a quick read, make the call, and keep moving!

## Built for photographer workflow

The workflow is designed around how people actually sort photos after a shoot.

You cull through copied previews in contained folders, and LVS reconciles those choices with the RAWs afterward. That keeps the process lightweight during the first pass and still gives you a reliable link back to the source files. While you are flicking through JPEG previews, you can still open the matching RAW whenever a frame deserves a closer look.

LVS also sets up the FastStone database automatically, so the viewer side feels like part of the same system rather than a manual setup step you have to repeat.

## How culling works

1. Browse the JPEG previews in FastStone.
2. Press 1–5 to keep and sort quickly.
3. Use the overlay to glance at AI guidance, captions, and color cues.
4. Open the RAW directly when you need to inspect the original.
5. Let LVS reconcile previews, RAWs, and selection folders into the final result.

## Current status

AutoCull is still a mockup. Even when it becomes real, I sincerely hope it stays the least-used feature in the project, because the whole point here is to make the human culling pass faster and less painful, not to replace it.

## Requirements

- Windows
- FastStone Image Viewer
- DigiKam support
- Python 3.10+
- PyQt6
- exiftool
- AutoHotkey v2

## License

AGPL-3.0-or-later
