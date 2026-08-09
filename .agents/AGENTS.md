# Project Rules

## Kernel State & Crash Safety Rule
- **Check Kernel State Post-Command**: On every single command/action executed against the target DUT (`dragon-fly`), ALWAYS verify the Linux kernel state using `dmesg | grep -iE 'oops|panic|bug:'` (must complete under 5 seconds).
- **Auto-Reboot on Oops/Panic**: If a kernel crash, panic, or oops is detected, IMMEDIATELY reboot the DUT (`reboot`) and wait for SSH recovery.
- **Probe Safety Enforcement**: ALWAYS verify driver instantiation (`aplay -l`) before opening `mtrace`. NEVER access `mtrace` on an unprobed driver.
- **Mtrace Non-Blocking Read Mandate**: NEVER open `mtrace` in blocking mode or run infinite background loops. All `mtrace` reads MUST use `O_NONBLOCK` wrapped in a shell `timeout 2s` command with synchronous tool execution (`WaitMsBeforeAsync=5000`), guaranteeing termination under 2 seconds.


