/* sidedoor-cli.exe (WB1): the console front door to the windowed sidedoor.exe beside it.

   sidedoor.exe is a GUI-subsystem program (no console window of its own), so a terminal does not wait
   for it. This console program runs sidedoor.exe with the same arguments and the same standard
   handles, waits for it and exits with its exit code: `sidedoor-cli doctor` and `--help` print in the
   terminal and return when done. Ctrl-C reaches sidedoor.exe through the shared console; this
   launcher ignores it and keeps waiting. Built by tools/build_windows.py with `zig cc`. */
#include <windows.h>
#include <wchar.h>

#define PATH_LIMIT 32768   /* long paths: GetModuleFileNameW's limit, not MAX_PATH */

static void fail(const char *what) {
    char text[200];
    DWORD written;
    int length = wsprintfA(text, "sidedoor-cli: %s (error %lu)\r\n", what, GetLastError());
    WriteFile(GetStdHandle(STD_ERROR_HANDLE), text, (DWORD)length, &written, NULL);
}

/* The arguments after argv[0] in the raw command line (CommandLineToArgvW's rule for argv[0]: up to
   the closing quote when it starts with one, else up to the first space or tab). */
static const wchar_t *arguments(const wchar_t *line) {
    if (*line == L'"') {
        line++;
        while (*line && *line != L'"') line++;
        if (*line) line++;
    } else {
        while (*line && *line != L' ' && *line != L'\t') line++;
    }
    return line;
}

int main(void) {
    static wchar_t target[PATH_LIMIT];
    DWORD length = GetModuleFileNameW(NULL, target, PATH_LIMIT);
    if (length == 0 || length >= PATH_LIMIT) { fail("cannot find its own path"); return 1; }
    wchar_t *slash = wcsrchr(target, L'\\');
    if (!slash || (size_t)(slash - target) + 14 > PATH_LIMIT) { fail("path too long"); return 1; }
    wcscpy(slash + 1, L"sidedoor.exe");

    const wchar_t *rest = arguments(GetCommandLineW());
    size_t size = wcslen(target) + wcslen(rest) + 3;
    wchar_t *line = (wchar_t *)HeapAlloc(GetProcessHeap(), 0, size * sizeof(wchar_t));
    if (!line) { fail("out of memory"); return 1; }
    _snwprintf(line, size, L"\"%ls\"%ls", target, rest);
    line[size - 1] = 0;

    STARTUPINFOW startup;
    ZeroMemory(&startup, sizeof startup);
    startup.cb = sizeof startup;
    startup.dwFlags = STARTF_USESTDHANDLES;
    DWORD ids[3] = {STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE};
    HANDLE *slots[3] = {&startup.hStdInput, &startup.hStdOutput, &startup.hStdError};
    for (int i = 0; i < 3; i++) {
        HANDLE handle = GetStdHandle(ids[i]);
        if (handle && handle != INVALID_HANDLE_VALUE)
            SetHandleInformation(handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT);
        *slots[i] = handle;
    }
    SetConsoleCtrlHandler(NULL, TRUE);

    PROCESS_INFORMATION process;
    if (!CreateProcessW(target, line, NULL, NULL, TRUE, 0, NULL, NULL, &startup, &process)) {
        fail("cannot start sidedoor.exe");
        return 1;
    }
    CloseHandle(process.hThread);
    WaitForSingleObject(process.hProcess, INFINITE);
    DWORD code = 1;
    GetExitCodeProcess(process.hProcess, &code);
    CloseHandle(process.hProcess);
    return (int)code;
}
