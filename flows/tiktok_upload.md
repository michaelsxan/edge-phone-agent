# TikTok upload flow (TikTok 47.4.3, moto g 5G 2024, 720×1612)

Verified by hand on 2026-11-02 00:38 with a private ("Only you") test post on @nomoreawkwardness.

1. **Copy the video to the gallery**
   `adb push video.mp4 /storage/emulated/0/DCIM/flagd/<name>.mp4`, then `touch` it and broadcast
   `android.intent.action.MEDIA_SCANNER_SCAN_FILE` with `file://<path>` so it shows first in Recents.
   Over Wi-Fi ADB this ran at ~0.5 MB/s (45 MB in 88 s).
2. **Open the camera**: bottom tab `Create` (resource id `ond`, bottom bar).
   Camera screen: record button `tx8` in the centre. Never tap it, Timer, or anything that starts a countdown.
3. **Open the gallery**: unlabeled thumbnail bottom-left (~70, 1510). Opens the Recents picker
   (tabs All / Videos / Photos / AI gallery).
4. **Pick the video**: first tile, top-left (~120, 360). Goes straight into the editor.
5. **Editor**: bottom row has `Your Story` (left, publishes immediately as a Story: never tap) and
   `Next` (right, ~530, 1512). Tap `Next`.
6. **Post screen**:
   - Description field `h9x` ("Add description..."): tap, type, press Back once to close the keyboard.
   - Visibility row `dud` "Everyone can view this post" opens a sheet: `Everyone` / `Friends` / `Only you`.
   - Ignore the Location suggestions (they're one-tap chips) and `Share to`.
   - `Drafts` (`gj_`) saves a draft; `Post` (`tjf`, ~533, 1507) publishes.
7. **After Post**: TikTok returns to the feed and uploads in the background; the video appears on
   Profile under its visibility tab within ~30 s (Only you → lock tab).

Resource ids are obfuscated and may change with app updates; prefer labels, fall back to ids.

## Deleting a post
Open it from Profile → tap the Share arrow (~664, 1332) → swipe the bottom row left twice →
`Delete` (`w64`) → in the next sheet tap `Delete` (not "Delete and re-edit"). Verified 2026-11-02.
