/* Linux launcher: replaces itself with the bundled Python running localcode.py, so arguments, stdio, signals and the exit code pass straight through. */
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    char self[PATH_MAX], python[PATH_MAX + 32], script[PATH_MAX + 32];
    ssize_t n = readlink("/proc/self/exe", self, sizeof self - 1); /* resolves symlinks, so a link on PATH works */
    if (n < 0) {
        perror("localcode: cannot find its own location");
        return 127;
    }
    self[n] = 0;
    char *slash = strrchr(self, '/');
    if (slash) *slash = 0;
    snprintf(python, sizeof python, "%s/runtime/bin/python3", self);
    snprintf(script, sizeof script, "%s/localcode.py", self);

    char **av = malloc((size_t)(argc + 4) * sizeof *av);
    if (!av) return 127;
    av[0] = python;
    av[1] = "-I"; /* ignore PYTHON* variables and user site-packages so the bundled runtime stays self-contained */
    av[2] = script;
    memcpy(av + 3, argv + 1, (size_t)argc * sizeof *av); /* argv[1..argc], including the closing NULL */
    execv(python, av);
    perror(python);
    return 127;
}
