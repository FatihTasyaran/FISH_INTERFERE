# fish_nproc — make the executor pool follow the CPUs we actually gave it

`rclcpp::executors::MultiThreadedExecutor` is constructed with
`number_of_threads = 0` by `component_container_mt`, which means
`std::thread::hardware_concurrency()`.  On glibc that is
`sysconf(_SC_NPROCESSORS_ONLN)` — the host's **online** CPUs, which ignores the
affinity mask.  There is no ROS or rclcpp environment variable for it.

Measured on odachi (i7-12700H, 20 logical CPUs, glibc 2.35):

    $ taskset -c 0,2,4,6,8,10 ./hardware_concurrency_probe
    hardware_concurrency=20  _SC_NPROCESSORS_ONLN=20  affinity=6

So a run pinned to 6 P-cores still starts 20 executor threads per MT container.
In session fish_20260906_151532 (Autoware, `taskset -c 0,2,4,6,8,10`) that shows
up as 20-40 threads per container, a peak of 21 callbacks in flight at once and
5.0 % of wall time with more callbacks in flight than CPUs.

## Build and use

    gcc -O2 -shared -fPIC -o libfish_nproc.so fish_nproc.c -ldl
    LD_PRELOAD=/path/libfish_nproc.so taskset -c 0,2,4,6,8,10 ros2 launch ...

    $ LD_PRELOAD=./libfish_nproc.so taskset -c 0,2,4,6,8,10 ./hardware_concurrency_probe
    hardware_concurrency=6   _SC_NPROCESSORS_ONLN=6   affinity=6
    $ FISH_NPROC=4 LD_PRELOAD=./libfish_nproc.so ./hardware_concurrency_probe
    hardware_concurrency=4

`FISH_NPROC` overrides explicitly; otherwise the shim reports
`CPU_COUNT(sched_getaffinity())`, so it also follows a Docker `--cpuset-cpus`.

## Status

**Not wired into the FISH entrypoint.**  Loading it changes the system under
measurement (fewer executor threads → different scheduling), so a campaign that
uses it must re-run *all* modes; results are not comparable with the runs made
without it.
