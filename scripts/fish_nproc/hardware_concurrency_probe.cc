#include <cstdio>
#include <thread>
#include <unistd.h>
#include <sched.h>
int main(){
  cpu_set_t m; CPU_ZERO(&m); sched_getaffinity(0,sizeof(m),&m);
  printf("hardware_concurrency=%u  _SC_NPROCESSORS_ONLN=%ld  _SC_NPROCESSORS_CONF=%ld  affinity=%d\n",
         std::thread::hardware_concurrency(),
         sysconf(_SC_NPROCESSORS_ONLN), sysconf(_SC_NPROCESSORS_CONF), CPU_COUNT(&m));
  return 0;
}
