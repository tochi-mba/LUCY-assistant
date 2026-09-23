# Music

Find, queue and play. Names are names, never a host.

`music.find` resolves a loosely specified track. `music.play` starts it on a connected
device; omit `device_id` to use the person's default speaker. `music.queue` adds one.
`music.pause` stops what is playing. Playback is something other people can hear, so it
asks unless they already allowed `music.control`.
