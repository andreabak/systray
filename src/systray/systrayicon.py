import os
import threading
import uuid
from copy import copy

from .win32_adapter import *


class MenuOption:
    def __init__(self, text, icon_path=None, callback=None, submenu=None):
        self.text = text
        self.icon_path = icon_path
        self.callback = callback
        self.submenu = submenu

        self.ftype = None
        self.fstate = None

        self.action_id = None
        self.menu_handle = None
        self.menu_position = None

    def refresh(self):
        pass


class CheckBoxMenuOption(MenuOption):
    # TODO: Review interface: allow passing only callable|bool, make checked a read-only property -> _get_checked
    def __init__(self, *args, check_hook=None, checked=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.check_hook = check_hook
        self.checked = checked
        self._get_checked()

    def refresh(self):
        self._get_checked()
        if self.menu_handle is not None:
            menu_item_checked = GetMenuState(self.menu_handle, self.menu_position, MF_BYPOSITION) & MFS_CHECKED
            if self.checked != menu_item_checked:
                u_check = MFS_CHECKED if self.checked else MFS_UNCHECKED
                CheckMenuItem(self.menu_handle, self.menu_position, MF_BYPOSITION | u_check)

    def _get_checked(self):
        if self.check_hook and callable(self.check_hook):
            self.checked = bool(self.check_hook())

    @property
    def fstate(self):
        self._get_checked()
        return MFS_CHECKED if self.checked else MFS_UNCHECKED

    @fstate.setter
    def fstate(self, value):
        return  # Does nothing


class SysTrayIcon(object):
    """
    menu_options: list or tuple of MenuOption objects or tuples of (text, icon_path, callback)

    text and tray hover text should be Unicode
    hover_text length is limited to 128; longer text will be truncated
    icon_path can be None
    callback must be a callable or special action from SysTrayIcon.SPECIAL_ACTIONS

    Can be used as context manager to enable automatic termination of tray
    if parent thread is closed:

        with SysTrayIcon(icon, hover_text) as systray:
            for item in ['item1', 'item2', 'item3']:
                systray.update(hover_text=item)
                do_something(item)

    """
    QUIT = 'QUIT'
    SPECIAL_ACTIONS = [QUIT]

    FIRST_ID = 1023

    def __init__(self,
                 icon,
                 hover_text,
                 menu_options=None,
                 on_quit=None,
                 default_menu_index=None,
                 window_class_name=None):

        self._icon = icon
        self._icon_shared = False
        self._hover_text = hover_text
        self._on_quit = on_quit

        self._next_action_id = SysTrayIcon.FIRST_ID
        self._menu_actions_by_id = dict()
        self._menu_options = list()
        self._prepare_menu_options(menu_options)

        window_class_name = window_class_name or ("SysTrayIconPy-%s" % (str(uuid.uuid4())))

        self._default_menu_index = (default_menu_index or 0)
        self._window_class_name = encode_for_locale(window_class_name)
        self._message_dict = {RegisterWindowMessage("TaskbarCreated"): self._restart,
                              WM_DESTROY: self._destroy,
                              WM_CLOSE: self._destroy,
                              WM_COMMAND: self._command,
                              WM_USER+20: self._notify}
        self._notify_id = None
        self._message_loop_thread = None
        self._hwnd = None
        self._hicon = 0
        self._hinst = None
        self._window_class = None
        self._menu = None
        self._register_class()

    def __enter__(self):
        """Context manager so SysTray can automatically close"""
        self.start()
        return self

    def __exit__(self, *args):
        """Context manager so SysTray can automatically close"""
        self.shutdown()

    def WndProc(self, hwnd, msg, wparam, lparam):
        hwnd = HANDLE(hwnd)
        wparam = WPARAM(wparam)
        lparam = LPARAM(lparam)
        if msg in self._message_dict:
            self._message_dict[msg](hwnd, msg, wparam.value, lparam.value)
        return DefWindowProc(hwnd, msg, wparam, lparam)

    def _register_class(self):
        # Register the Window class.
        self._window_class = WNDCLASS()
        self._hinst = self._window_class.hInstance = GetModuleHandle(None)
        self._window_class.lpszClassName = self._window_class_name
        self._window_class.style = CS_VREDRAW | CS_HREDRAW
        self._window_class.hCursor = LoadCursor(0, IDC_ARROW)
        self._window_class.hbrBackground = COLOR_WINDOW
        self._window_class.lpfnWndProc = LPFN_WNDPROC(self.WndProc)
        RegisterClass(ctypes.byref(self._window_class))

    def _create_window(self):
        style = WS_OVERLAPPED | WS_SYSMENU
        self._hwnd = CreateWindowEx(0, self._window_class_name,
                                      self._window_class_name,
                                      style,
                                      0,
                                      0,
                                      CW_USEDEFAULT,
                                      CW_USEDEFAULT,
                                      0,
                                      0,
                                      self._hinst,
                                      None)
        UpdateWindow(self._hwnd)
        self._refresh_icon()

    def _message_loop_func(self):
        self._create_window()
        PumpMessages()

    def start(self):
        if self._hwnd:
            return      # already started
        self._message_loop_thread = threading.Thread(target=self._message_loop_func)
        self._message_loop_thread.start()

    def shutdown(self):
        if not self._hwnd:
            return      # not started
        PostMessage(self._hwnd, WM_CLOSE, 0, 0)
        self._message_loop_thread.join()

    def update(self, icon=None, hover_text=None):
        """ update icon image and/or hover text """
        if icon:
            self._icon = icon
            self._load_icon()
        if hover_text:
            self._hover_text = hover_text
        self._refresh_icon()

    def _prepare_menu_options(self, menu_options):
        menu_options = menu_options or ()
        if isinstance(menu_options, tuple):
            menu_options = list(menu_options)
        menu_options.append(MenuOption('Quit', callback=SysTrayIcon.QUIT))
        self._next_action_id = SysTrayIcon.FIRST_ID
        self._menu_actions_by_id = dict()
        self._menu_options = self._recompile_menu_options_with_ids(menu_options)

    def _recompile_menu_options_with_ids(self, menu_options):
        result = []
        for menu_option in menu_options:
            if isinstance(menu_option, tuple):
                menu_option = MenuOption(*menu_option)
            elif isinstance(menu_option, dict):
                menu_option = MenuOption(**menu_option)
            elif isinstance(menu_option, MenuOption):
                menu_option = copy(menu_option)
            else:
                raise ValueError('Unknown menu option type', type(menu_option))
            menu_option.action_id = self._next_action_id
            submenu = menu_option.submenu or _non_string_iterable(menu_option.callback)
            if callable(menu_option.callback) or menu_option.callback in SysTrayIcon.SPECIAL_ACTIONS:
                self._menu_actions_by_id[menu_option.action_id] = menu_option.callback
            elif submenu:
                menu_option.submenu = self._recompile_menu_options_with_ids(submenu)
                menu_option.callback = None
            else:
                raise Exception('Unknown item', menu_option.text, menu_option.icon_path, menu_option.callback)
            result.append(menu_option)
            self._next_action_id += 1
        return result

    def _load_icon(self):
        # release previous icon, if a custom one was loaded
        # note: it's important *not* to release the icon if we loaded the default system icon (with
        # the LoadIcon function) - this is why we assign self._hicon only if it was loaded using LoadImage
        if not self._icon_shared and self._hicon != 0:
            DestroyIcon(self._hicon)
            self._hicon = 0

        # Try and find a custom icon
        hicon = 0
        if self._icon is not None and os.path.isfile(self._icon):
            icon_flags = LR_LOADFROMFILE | LR_DEFAULTSIZE
            icon = encode_for_locale(self._icon)
            hicon = self._hicon = LoadImage(0, icon, IMAGE_ICON, 0, 0, icon_flags)
            self._icon_shared = False

        # Can't find icon file - using default shared icon
        if hicon == 0:
            self._hicon = LoadIcon(0, IDI_APPLICATION)
            self._icon_shared = True
            self._icon = None

    def _refresh_icon(self):
        if self._hwnd is None:
            return
        if self._hicon == 0:
            self._load_icon()
        if self._notify_id:
            message = NIM_MODIFY
        else:
            message = NIM_ADD
        self._notify_id = NotifyData(self._hwnd,
                          0,
                          NIF_ICON | NIF_MESSAGE | NIF_TIP,
                          WM_USER+20,
                          self._hicon,
                          self._hover_text)
        Shell_NotifyIcon(message, ctypes.byref(self._notify_id))

    def _restart(self, hwnd, msg, wparam, lparam):
        self._refresh_icon()

    def _destroy(self, hwnd, msg, wparam, lparam):
        if self._on_quit:
            self._on_quit(self)
        nid = NotifyData(self._hwnd, 0)
        Shell_NotifyIcon(NIM_DELETE, ctypes.byref(nid))
        PostQuitMessage(0)  # Terminate the app.
        # TODO * release self._menu with DestroyMenu and reset the memeber
        #      * release self._hicon with DestoryIcon and reset the member
        #      * release loaded menu icons (loaded in _load_menu_icon) with DeleteObject
        #        (we don't keep those objects anywhere now)
        self._hwnd = None
        self._notify_id = None

    def _notify(self, hwnd, msg, wparam, lparam):
        if lparam == WM_LBUTTONDBLCLK:
            self._execute_menu_option(self._default_menu_index + SysTrayIcon.FIRST_ID)
        elif lparam == WM_RBUTTONUP:
            self._show_menu()
        elif lparam == WM_LBUTTONUP:
            pass
        return True

    def _refresh_menu_options(self, menu_options=None):
        if menu_options is None:
            menu_options = self._menu_options
        for menu_option in menu_options:
            menu_option.refresh()
            if menu_option.submenu:
                self._refresh_menu_options(menu_option.submenu)

    def _show_menu(self):
        if self._menu is None:
            self._menu = CreatePopupMenu()
            self._create_menu(self._menu, self._menu_options)
            #SetMenuDefaultItem(self._menu, 1000, 0)

        self._refresh_menu_options()

        pos = POINT()
        GetCursorPos(ctypes.byref(pos))
        # See http://msdn.microsoft.com/library/default.asp?url=/library/en-us/winui/menus_0hdi.asp
        SetForegroundWindow(self._hwnd)
        TrackPopupMenu(self._menu,
                       TPM_LEFTALIGN,
                       pos.x,
                       pos.y,
                       0,
                       self._hwnd,
                       None)
        PostMessage(self._hwnd, WM_NULL, 0, 0)

    def _create_menu(self, menu, menu_options):
        for position, menu_option in enumerate(menu_options):
            option_icon = self._prep_menu_icon(menu_option.icon_path) if menu_option.icon_path else None

            item_attributes = dict(text=menu_option.text,
                                   hbmpItem=option_icon,
                                   fType=menu_option.ftype,
                                   fState=menu_option.fstate)
            if menu_option.action_id in self._menu_actions_by_id:
                item = PackMENUITEMINFO(**item_attributes, wID=menu_option.action_id)
            elif menu_option.submenu is not None:
                submenu = CreatePopupMenu()
                self._create_menu(submenu, menu_option.submenu)
                item = PackMENUITEMINFO(**item_attributes, hSubMenu=submenu)
            else:
                raise ValueError('Bad configured menu option: no action nor submenu found')
            menu_option.menu_handle = menu
            menu_option.menu_position = position
            InsertMenuItem(menu, position, True, ctypes.byref(item))

    @staticmethod
    def _prep_menu_icon(icon):
        icon = encode_for_locale(icon)
        # First load the icon.
        ico_x = GetSystemMetrics(SM_CXSMICON)
        ico_y = GetSystemMetrics(SM_CYSMICON)
        hicon = LoadImage(0, icon, IMAGE_ICON, ico_x, ico_y, LR_LOADFROMFILE)

        hdcBitmap = CreateCompatibleDC(None)
        hdcScreen = GetDC(None)
        hbm = CreateCompatibleBitmap(hdcScreen, ico_x, ico_y)
        hbmOld = SelectObject(hdcBitmap, hbm)
        # Fill the background.
        brush = GetSysColorBrush(COLOR_MENU)
        FillRect(hdcBitmap, ctypes.byref(RECT(0, 0, 16, 16)), brush)
        # draw the icon
        DrawIconEx(hdcBitmap, 0, 0, hicon, ico_x, ico_y, 0, 0, DI_NORMAL)
        SelectObject(hdcBitmap, hbmOld)

        # No need to free the brush
        DeleteDC(hdcBitmap)
        DestroyIcon(hicon)

        return hbm

    def _command(self, hwnd, msg, wparam, lparam):
        _id = LOWORD(wparam)
        self._execute_menu_option(_id)

    def _execute_menu_option(self, action_id):
        menu_action = self._menu_actions_by_id[action_id]
        if menu_action == SysTrayIcon.QUIT:
            DestroyWindow(self._hwnd)
        else:
            menu_action(self)


def _non_string_iterable(obj):
    try:
        iter(obj)
    except TypeError:
        return False
    else:
        return not isinstance(obj, str)
