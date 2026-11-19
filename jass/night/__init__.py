"""`jass night`: keep unattended agent work running. Nothing here changes a system setting; apart from its own log
and lock, the only thing it writes is a message to a watched thread, sent by `watch`.

- `probe` reads this box: sleep policy, power, network, memory, temp folder, disk.
- `t3state` reads T3 Code's store and says what each thread is doing.
- `check` turns both into the pre-flight report (`jass night check`).
- `watch` is the watchdog loop (`jass night watch`); `heavy` caps one heavy command (`jass heavy`).
"""
