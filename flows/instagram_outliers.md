# Instagram niche outliers (outliers.py) — verified 2026-11-03 21:10 (Instagram 452.1.0.53.64)

Read-only: never likes, follows, saves or sends. Runs on the practice account.

- **Open a profile**: `am start -a android.intent.action.VIEW -d https://www.instagram.com/<handle>/ com.instagram.android`.
  Verify `action_bar_title` == handle. Missing/private profiles show "isn't available" / "This account is private".
- **Profile tabs**: `profile_tab_icon_view` with labels Grid view / Reels / Reposted / Photos of you. Tap "Reels".
- **Grid tiles**: `preview_clip_thumbnail`, label "Reel by <handle>. View Count 331K. Double tap to play or pause."
  (also `preview_clip_play_count` text). Three per row, newest first. Pinned reels sit first too.
- **Scroll**: slow drag `input swipe 360 1300 360 500 900` (no fling). Tiles have no ids: after a scroll, new tiles
  are found by matching the overlap of view-count texts.
- **Open a tile**: single tap (a double tap likes). The reel screen has `clips_author_username`; frames are grabbed
  right after it appears and 1.5 s later (opening hook).
- **Search results** ("For you" tab): tiles "Reel by <display name> at row r, column c" + `preview_clip_play_count`.
  The display name is not the handle: open the reel and read `clips_author_username`.
- Profile header also has `row_profile_header_button_chaining` ("Similar accounts"): not used yet.
