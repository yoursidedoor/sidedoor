"""The computer's own unlock, asked for on a click (E15.T7: Show password on the Saved sign-ins page).

`available()`: can this computer confirm the person at it. `verify(reason)`: ask now, and answer True only
when the system says the person passed. Nothing is stored and no keychain item is involved.
- macOS: LocalAuthentication's device-owner policy (Touch ID, Apple Watch or the Mac's password), loaded
  through pyobjc-core, which the app already ships (no framework binding package).
- Windows: the Windows Security credential prompt for the signed-in user (credui, ctypes), and what was
  typed is checked with LogonUserW. The typed password is wiped from memory and never leaves this call.
- Anywhere else, or when either cannot be loaded: not available, and `verify` is False.
Never from the tests: like credentials.py's real stores, both refuse while unittest is loaded (a test
patches `available` and `verify`). Any failure is False; nothing here raises.
"""

import sys
import threading

import credentials


WAIT_SECONDS = 120
MAC_FRAMEWORK = "/System/Library/Frameworks/LocalAuthentication.framework"
MAC_POLICY = 2                    # LAPolicyDeviceOwnerAuthentication: biometrics, watch or the password
MAC_REASON = "show a saved password"                       # macOS writes: "Sidedoor" is trying to ...
WINDOWS_CAPTION = "Sidedoor"
WINDOWS_MESSAGE = "Confirm it's you to show a saved password."


def _allowed():
    return credentials._real_store_allowed()


def _mac_context():
    import objc
    found = {}
    objc.loadBundle("LocalAuthentication", found, bundle_path=MAC_FRAMEWORK)
    objc.registerMetaDataForSelector(b"LAContext", b"evaluatePolicy:localizedReason:reply:", {"arguments": {
        4: {"callable": {"retval": {"type": b"v"}, "arguments": {0: {"type": b"^v"}, 1: {"type": b"Z"}, 2: {"type": b"@"}}}}}})
    return found["LAContext"].alloc().init()


def _mac_available():
    answer = _mac_context().canEvaluatePolicy_error_(MAC_POLICY, None)
    return bool(answer[0] if isinstance(answer, tuple) else answer)


def _mac_verify():
    context = _mac_context()
    done, passed = threading.Event(), []

    def reply(success, error):
        passed.append(bool(success))
        done.set()
    context.evaluatePolicy_localizedReason_reply_(MAC_POLICY, MAC_REASON, reply)
    if not done.wait(WAIT_SECONDS):
        context.invalidate()
        return False
    return passed == [True]


def _windows_verify():
    import ctypes
    from ctypes import wintypes

    class Info(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hwndParent", wintypes.HWND), ("pszMessageText", wintypes.LPCWSTR),
                    ("pszCaptionText", wintypes.LPCWSTR), ("hbmBanner", wintypes.HANDLE)]

    credui = ctypes.WinDLL("credui", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32")
    info = Info(ctypes.sizeof(Info), None, WINDOWS_MESSAGE, WINDOWS_CAPTION, None)
    package, buffer, size, save = wintypes.ULONG(0), ctypes.c_void_p(), wintypes.ULONG(0), wintypes.BOOL(False)
    credui.CredUIPromptForWindowsCredentialsW.argtypes = [
        ctypes.POINTER(Info), wintypes.DWORD, ctypes.POINTER(wintypes.ULONG), ctypes.c_void_p, wintypes.ULONG,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG), ctypes.POINTER(wintypes.BOOL), wintypes.DWORD]
    credui.CredUIPromptForWindowsCredentialsW.restype = wintypes.DWORD
    if credui.CredUIPromptForWindowsCredentialsW(ctypes.byref(info), 0, ctypes.byref(package), None, 0, ctypes.byref(buffer),
                                                 ctypes.byref(size), ctypes.byref(save), 0x200) != 0:   # current user only
        return False                                                                                    # cancelled, or an error
    user, domain, password = (ctypes.create_unicode_buffer(514) for _ in range(3))
    lengths = [wintypes.DWORD(514) for _ in range(3)]
    try:
        credui.CredUnPackAuthenticationBufferW.argtypes = [
            wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        credui.CredUnPackAuthenticationBufferW.restype = wintypes.BOOL
        if not credui.CredUnPackAuthenticationBufferW(1, buffer, size.value, user, ctypes.byref(lengths[0]), domain,
                                                      ctypes.byref(lengths[1]), password, ctypes.byref(lengths[2])):
            return False
        name, realm = user.value, domain.value or None
        if realm is None and "\\" in name:
            realm, name = name.split("\\", 1)
        token = wintypes.HANDLE()
        advapi32.LogonUserW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                        wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.LogonUserW.restype = wintypes.BOOL
        if not advapi32.LogonUserW(name, realm, password.value, 2, 0, ctypes.byref(token)):   # interactive, default provider
            return False
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(token)
        return True
    finally:
        ctypes.memset(password, 0, ctypes.sizeof(password))
        if buffer.value:
            ctypes.memset(buffer, 0, size.value)
            ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            ole32.CoTaskMemFree(buffer)


def available():
    """True when this computer can ask the person at it to prove who they are."""
    try:
        if not _allowed():
            return False
        if sys.platform == "darwin":
            return _mac_available()
        if sys.platform == "win32":
            import ctypes
            ctypes.WinDLL("credui")
            return True
    except Exception:
        return False
    return False


def verify():
    """Ask now. True only when the system confirms the person; a cancel, a timeout or any error is False."""
    try:
        if not _allowed():
            return False
        if sys.platform == "darwin":
            return _mac_verify()
        if sys.platform == "win32":
            return _windows_verify()
    except Exception:
        return False
    return False
