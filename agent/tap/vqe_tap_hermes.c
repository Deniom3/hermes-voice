// Перехват обработанного канала VQE на Станции Лайт 2 (plum/prune).
//
// Штатный maind --platformd вызывает YandexVqeC_GetOutputChannelData из
// libyandex_evil.so и получает уже очищенный от эха/шума поток vqe_0.
// Модуль только читает КОПИЮ этого буфера: исходный вызов, его аргументы,
// возвращаемое значение и errno остаются нетронутыми, поэтому штатная Алиса
// продолжает работать. Копия уходит в Unix-датаграммы, наш агент её читает.
//
// Включается только при заданной HERMES_VQE_SOCKET, то есть по отдельному
// профилю запуска. Без переменной модуль пассивен.
//
// Сборка (userspace колонки 32-битный armhf, glibc 2.30):
//   arm-linux-gnueabihf-gcc -O2 -fPIC -shared -Wall -Wextra \
//       -o vqe_tap_hermes.so vqe_tap_hermes.c -ldl

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

typedef void *(*vqe_output_fn)(void *engine, unsigned int kind,
                               unsigned int index, const float **data,
                               size_t *frames);

#define VQE_CHANNEL_TYPE 6u /* AudioInputChannelType: обработанный VQE */
#define VQE_CHANNEL_INDEX 0u /* vqe_0 */
#define TAP_MAX_FRAMES 4096u
#define TAP_PACKET_FRAMES 160u /* 10 мс при 16 кГц */

static vqe_output_fn g_next;
static int g_fd = -1;
static struct sockaddr_un g_addr;
static socklen_t g_addrlen;
static int g_ready;
static unsigned long g_sent, g_dropped;

static void tap_init(void)
{
    const char *path = getenv("HERMES_VQE_SOCKET");

    g_ready = 1;
    if (!path || !*path)
        return; /* пассивный режим: профиль не просил перехват */
    if (strlen(path) >= sizeof(g_addr.sun_path))
        return;

    g_fd = socket(AF_UNIX, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (g_fd < 0)
        return;

    memset(&g_addr, 0, sizeof(g_addr));
    g_addr.sun_family = AF_UNIX;
    memcpy(g_addr.sun_path, path, strlen(path));
    g_addrlen = (socklen_t)(offsetof(struct sockaddr_un, sun_path) + strlen(path) + 1);
}

static void tap_send(const float *src, size_t frames)
{
    int16_t packet[TAP_PACKET_FRAMES];
    size_t offset = 0;

    while (offset < frames) {
        size_t count = frames - offset;
        size_t i;

        if (count > TAP_PACKET_FRAMES)
            count = TAP_PACKET_FRAMES;

        for (i = 0; i < count; ++i) {
            float v = src[offset + i];

            // Нормализованный float32 -> S16_LE, как у штатного потребителя:
            // масштаб 32768, насыщение по краям, NaN -> тишина.
            if (v != v)
                packet[i] = 0;
            else if (v >= 1.0f)
                packet[i] = 32767;
            else if (v <= -1.0f)
                packet[i] = -32768;
            else
                packet[i] = (int16_t)(v * 32768.0f);
        }

        // Медленный или отсутствующий приёмник не должен задерживать
        // штатный аудиопоток: неблокирующий сокет, при переполнении кадр
        // отбрасывается.
        if (sendto(g_fd, packet, count * sizeof(int16_t), MSG_DONTWAIT,
                   (const struct sockaddr *)&g_addr, g_addrlen) < 0)
            ++g_dropped;
        else
            ++g_sent;

        offset += count;
    }
}

void *YandexVqeC_GetOutputChannelData(void *engine, unsigned int kind,
                                     unsigned int index, const float **data,
                                     size_t *frames)
{
    int entered_errno = errno;
    void *result;
    int original_errno;

    if (!g_next) {
        g_next = (vqe_output_fn)dlsym(RTLD_NEXT, "YandexVqeC_GetOutputChannelData");
        errno = entered_errno;
    }

    // Оригинальный символ обязан быть: без него модуль бессмысленен, но и
    // ронять демон нельзя — просто возвращаем ошибку вызывающему.
    if (!g_next) {
        errno = ENOSYS;
        return NULL;
    }

    result = g_next(engine, kind, index, data, frames);
    original_errno = errno;

    if (!result && kind == VQE_CHANNEL_TYPE && index == VQE_CHANNEL_INDEX &&
        data && frames && *data && *frames > 0 && *frames <= TAP_MAX_FRAMES) {
        if (!g_ready)
            tap_init();
        if (g_fd >= 0)
            tap_send(*data, *frames);
    }

    errno = original_errno;
    return result;
}

// Диагностика для лога демона: сколько кадров ушло и сколько потеряно.
__attribute__((destructor)) static void tap_report(void)
{
    if (g_sent || g_dropped) {
        char buf[96];
        int n = snprintf(buf, sizeof(buf),
                         "HERMES_VQE tap: sent=%lu dropped=%lu\n",
                         g_sent, g_dropped);
        if (n > 0)
            (void)!write(2, buf, (size_t)n);
    }
}
