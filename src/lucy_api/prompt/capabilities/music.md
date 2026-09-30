# Music

Find, queue and play. Names are names, never a host.

`music.find` resolves a loosely specified track. `music.play` starts it on a connected
device, and takes what `music.find` found by reference -- find and play in one plan:

    {"steps": [
      {"id": "found", "op": "music.find", "input": {"name": "Clair de lune", "artist": "Debussy"}},
      {"id": "play", "op": "music.play", "input": {"track": "$found"}}
    ]}

A `uri` is for a track you already hold; a `$` reference in `uri` is refused. Omit
`device_id` to use the person's default speaker. `music.queue` adds a track the same way,
and answers per track when given several: which were queued, which were refused and why,
and which were left for a new step when time ran short. `music.pause` stops what is playing. Playback is something other people can hear, so it
asks unless they already allowed `music.control`.
