# Automated Mission for AAS

FISH includes scripts for reproducible trace capture with scripted action
sequences, designed for the
[Aerial Autonomy Stack (AAS)](https://github.com/JacopoPan/aerial-autonomy-stack)
drone simulation platform.

## `scripts/fish_mission.sh`

Sends a predefined mission (takeoff → orbit → land) to the aircraft container
via `docker exec`. Runs on the **host** while `sim_run.sh` is active.

```bash
./scripts/fish_mission.sh                       # default container name
./scripts/fish_mission.sh <container_name>      # custom container name
INSTANCE=1 ./scripts/fish_mission.sh            # for multi-instance
```

The mission sequence:
1. Wait for nodes to initialize (polls `ros2 node list`)
2. Wait for action servers (polls `ros2 action list`)
3. Settle time (30s for PX4 SITL GPS fix + EKF convergence)
4. **Takeoff** (altitude: 40m)
5. Wait 10s
6. **Orbit** (east: 200m, altitude: 80m, radius: 100m)
7. Wait 10s
8. **Land** (altitude: 10m)
9. Wait 10s
10. Write manifest to `/tmp/fish_mission_*.log`

## `scripts/fish_auto_mission.sh`

Full automated session: starts simulation, runs mission, stops everything.
Single command, zero interaction.

```bash
./scripts/fish_auto_mission.sh
```

Flow:
1. Starts `sim_run.sh` in background (via named pipe for stdin control)
2. Waits for aircraft container to come up
3. Runs `scripts/fish_mission.sh`
4. Sends keystroke to `sim_run.sh` to trigger shutdown
5. FISH stop → snapshot → trace save → container cleanup

## Output

After completion:
- Trace data: `~/fish_traces/` (latest session)
- Mission manifest: `/tmp/fish_mission_*.log`

The manifest logs each action with UTC timestamps:

```
2026-03-30T23:32:38.525Z  START  mission
2026-03-30T23:32:38.531Z  SEND  takeoff_action  {takeoff_altitude: 40.0}
2026-03-30T23:32:48.437Z  OK    takeoff_action  SUCCEEDED
2026-03-30T23:32:58.442Z  SEND  orbit_action  {east: 200.0, ...}
2026-03-30T23:33:00.781Z  OK    orbit_action  SUCCEEDED
2026-03-30T23:33:10.787Z  SEND  land_action  {landing_altitude: 10.0}
2026-03-30T23:33:23.634Z  OK    land_action  SUCCEEDED
2026-03-30T23:33:33.639Z  END    mission
```

## `scripts/build_fish_image.sh`

Automates the FISH image build process. Starts a temporary container,
copies `fish_interfere/`, runs `scripts/setup_fish.sh --yes`, and commits.

```bash
./scripts/build_fish_image.sh                   # default: aircraft-image:latest
./scripts/build_fish_image.sh <base_image>      # custom base image
```

## Simulation command

The simulation is started with:

```bash
AUTOPILOT=px4 NUM_QUADS=1 NUM_VTOLS=0 WORLD=swiss_town \
  HEADLESS=false RTF=3.0 ./sim_run.sh
```
