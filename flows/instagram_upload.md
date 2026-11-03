# Instagram Reel upload flow (Instagram 452.1.0.53.64, moto g 5G 2024, 720×1612)

Verified by hand on 2026-11-02 00:54 with a Close Friends-only test reel on @nomoreawkwardness
(Close Friends list empty, so only the account itself could see it), then deleted.

1. **Copy the video to the gallery**: same as TikTok (`adb push` to DCIM/flagd, `touch`, media-scan broadcast).
2. **Open Create**: Home screen, top-left `+` (`action_bar_left_button`, [0,80][84,178]).
   Opens "New reel" gallery with tabs POST / STORY / REEL / LIVE at the bottom; REEL is selected by default.
3. **Pick the video**: tiles are `gallery_grid_item_thumbnail` with content-desc
   "Unselected Video thumbnail created on <date>…". Tile 1 is "Open camera"; the newest video is tile 2 (~360, 585).
   Tapping it goes straight to the reel editor.
4. **Editor**: `Next` = `clips_right_action_button` (bottom right). Don't touch Add audio/text/voice/stickers.
5. **Share screen** ("New reel", `ModalActivity`):
   - Caption: `caption_input_text_view` ("Write a caption and add hashtags…"). Tap, type, then tap `OK` (top right) to commit.
   - Scroll down for: `Audience` row (Everyone / Close Friends), `Facebook` toggle, `Your story` toggle,
     `Add AI label` toggle. All toggles must stay **off** (verify `checked="false"` on the `toggle` nodes).
   - Ignore `Hashtags`/`Poll`/`Prompt` chips, `Tag people`, `Add location`.
   - `Save draft` (`save_draft_button`) vs `Share` (`share_button`). Share publishes.
6. **After Share**: Instagram jumps to the Reels feed and uploads in the background (~30–40 s). The new reel shows on
   Profile → Reels tab as "Reel by <account>. View Count 0." and at the top of the Home feed.
   A "Rate Instagram" dialog may appear: tap `No, thanks`.

## Deleting a reel
Home feed (or open it from Profile) → post menu (≡, top-right of the post, ~680, 453) → `Delete` (bottom, red) →
"Delete Post?" → `Delete`. The same menu has `View insights` (per-post stats) and `Archive`.

## Notes
- Close Friends reels do count in the profile's post total (61 → 62 while one was up).
- The mapper's 2026-11-01 "Start new video" tap created a draft ("1101", Nov 1 9:10 PM); deleted 2026-11-02 via
  Create → Drafts → ⋯ → Delete → Delete. The user's own drafts (Oct 14, Oct 21) were left alone.

## Automated (instagram_upload.py) — passed end to end 2026-11-02 17:35
Lessons baked into the script:
- Gallery tiles appear a few seconds before they accept taps: wait, then retry the tap once.
- An aborted run leaves an unfinished reel; next time Create asks "Keep editing your draft?". Never pick
  "Start new video" (saves a draft). Pick Keep editing, then Back until **Start over** and tap it.
- Clear the caption field before typing (a restored session may already have text).
- The phone screen sleeps during waits: wake before every check. Never force-stop Instagram after Share.
- Verify on Profile → Reels tab (2nd tab, no label). Tapping it while already selected opens a
  Latest/Most viewed sort menu: close it with Back. Pull down to refresh while waiting for the upload.
- TikTok saves a copy of every posted video to DCIM/Camera; delete pushed files *and* their MediaStore rows.
