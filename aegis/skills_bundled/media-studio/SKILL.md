---
name: media-studio
description: >
  Use when asked to create or generate media — images, pictures, thumbnails, logos, posters,
  illustrations, voice-overs, speech, songs, beats, music, or videos. Calls the media__ tools,
  which route across free backends automatically.
---

# Media Studio

## Images — `media__generate_image`
Write a rich prompt: subject, action, setting, style (photo, 3D, watercolour, flat vector),
lighting, lens/composition, colour palette, mood. Sizes: thumbnail 1280x720, square 1024x1024,
portrait 768x1344, banner 1536x640. Offer 2-4 variations (`count`) when the user is exploring.
Never recreate a known character, logo or copyrighted artwork — make an original instead.

## Speech — `media__generate_speech`
Clean the text for reading aloud (expand abbreviations, numbers in words where natural).

## Music — `media__generate_music`
Give genre, sub-genre, BPM, instruments, vocal type, language and mood. Check `me__about('interests')` for the user's favourite styles. Write original
lyrics in the user's language (mixes welcome). If the result is a Suno pack, tell the user it is ready
to paste into Suno Custom mode.

## Video — `media__generate_video`
Describe shots in order (scene, camera move, style) and total length. Add `narration` for a
voice-over. If the fallback slideshow was used, say so plainly.

After generating, the media is already shown in the chat — describe it briefly and offer one
concrete tweak rather than repeating the file path.
