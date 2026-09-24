"""Isolated Win32 client-window capture; never falls back to desktop pixels."""
from __future__ import annotations
import ctypes as c
from ctypes import wintypes as w
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from PIL import Image


def api():
    user = c.WinDLL('user32', use_last_error=True)
    gdi = c.WinDLL('gdi32', use_last_error=True)
    signatures = [
        (user,'GetDC',[w.HWND],w.HDC), (user,'ReleaseDC',[w.HWND,w.HDC],c.c_int),
        (user,'IsWindow',[w.HWND],w.BOOL), (user,'IsWindowVisible',[w.HWND],w.BOOL),
        (user,'IsIconic',[w.HWND],w.BOOL),
        (user,'GetClientRect',[w.HWND,c.POINTER(w.RECT)],w.BOOL),
        (user,'GetWindowTextLengthW',[w.HWND],c.c_int),
        (user,'GetWindowTextW',[w.HWND,w.LPWSTR,c.c_int],c.c_int),
        (user,'GetWindowThreadProcessId',[w.HWND,c.POINTER(w.DWORD)],w.DWORD),
        (user,'PrintWindow',[w.HWND,w.HDC,w.UINT],w.BOOL),
        (gdi,'CreateCompatibleDC',[w.HDC],w.HDC),
        (gdi,'CreateCompatibleBitmap',[w.HDC,c.c_int,c.c_int],w.HBITMAP),
        (gdi,'SelectObject',[w.HDC,w.HANDLE],w.HANDLE),
        (gdi,'DeleteObject',[w.HANDLE],w.BOOL), (gdi,'DeleteDC',[w.HDC],w.BOOL),
        (gdi,'GetDIBits',[w.HDC,w.HBITMAP,w.UINT,w.UINT,c.c_void_p,c.c_void_p,w.UINT],c.c_int),
    ]
    for lib,name,args,result in signatures:
        fn=getattr(lib,name); fn.argtypes=args; fn.restype=result
    return user,gdi


def windows():
    user,_=api()
    result=[]
    callback_type=c.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
    @callback_type
    def visit(hwnd,_):
        n=user.GetWindowTextLengthW(hwnd)
        if n and user.IsWindowVisible(hwnd):
            title=c.create_unicode_buffer(n+1); user.GetWindowTextW(hwnd,title,n+1)
            pid=w.DWORD(); user.GetWindowThreadProcessId(hwnd,c.byref(pid))
            result.append(dict(handle=int(hwnd),pid=pid.value,title=title.value,minimized=bool(user.IsIconic(hwnd))))
        return True
    user.EnumWindows.argtypes=[callback_type,w.LPARAM]
    user.EnumWindows(visit,0)
    return result


def select_window(selector, candidates=None):
    from media_support import MediaError
    candidates=windows() if candidates is None else candidates
    selector=str(selector)
    matches=[x for x in candidates if str(x['handle'])==selector or x['title'].casefold()==selector.casefold()]
    if not matches:
        matches=[x for x in candidates if selector.casefold() in x['title'].casefold()]
    if len(matches)!=1:
        raise MediaError('Window not found or ambiguous. Use media_cli.py windows and select its exact title or handle.')
    if matches[0]['minimized']:
        raise MediaError('The selected window is minimized. Restore it before capture.')
    return matches[0]


class WindowCapture:
    def __init__(self, selector):
        from media_support import MediaError
        self.error=MediaError
        self.user,self.gdi=api()
        try: self.user.SetProcessDpiAwarenessContext(c.c_void_p(-4))
        except AttributeError: pass
        self.selected=select_window(selector)
        self.monitors=[{},self.bounds()]

    def bounds(self):
        hwnd=self.selected['handle']; pid=w.DWORD()
        self.user.GetWindowThreadProcessId(hwnd,c.byref(pid))
        if not self.user.IsWindow(hwnd) or pid.value!=self.selected['pid'] or self.user.IsIconic(hwnd):
            raise self.error('Selected window closed, changed, or was minimized. Capture stopped.')
        rect=w.RECT()
        if not self.user.GetClientRect(hwnd,c.byref(rect)) or rect.right<2 or rect.bottom<2:
            raise self.error('Selected window has no capturable client area.')
        if rect.right*rect.bottom>40_000_000:
            raise self.error('Selected window exceeds the capture dimension limit.')
        return dict(left=0,top=0,width=rect.right,height=rect.bottom)

    def grab(self, bounds):
        actual=self.bounds()
        if actual!=self.monitors[1]:
            raise self.error('Window was resized during capture. Request a new capture.')
        width,height=actual['width'],actual['height']
        hwnd=self.selected['handle']; user,gdi=self.user,self.gdi
        dc=user.GetDC(hwnd); memory=gdi.CreateCompatibleDC(dc); bitmap=gdi.CreateCompatibleBitmap(dc,width,height)
        previous=None
        try:
            if not dc or not memory or not bitmap: raise self.error('Window capture resources unavailable.')
            previous=gdi.SelectObject(memory,bitmap)
            # PW_CLIENTONLY | PW_RENDERFULLCONTENT: request this application's pixels.
            if not user.PrintWindow(hwnd,memory,3): raise self.error('This game/window does not support window capture.')
            gdi.SelectObject(memory,previous); previous=None
            class Header(c.Structure):
                _fields_=[('size',w.DWORD),('width',w.LONG),('height',w.LONG),('planes',w.WORD),('bits',w.WORD),('compression',w.DWORD),('image_size',w.DWORD),('x',w.LONG),('y',w.LONG),('colors',w.DWORD),('important',w.DWORD)]
            header=Header(40,width,-height,1,32,0,0,0,0,0,0)
            pixels=c.create_string_buffer(width*height*4)
            if gdi.GetDIBits(memory,bitmap,0,height,pixels,c.byref(header),0)!=height:
                raise self.error('Window pixel read failed.')
            image=Image.frombytes('RGB',(width,height),pixels.raw,'raw','BGRX')
            if max(high for low,high in image.getextrema())==0:
                raise self.error('Window capture returned a black frame. Try windowed/borderless mode; no desktop fallback was used.')
            x,y=bounds['left'],bounds['top']
            image=image.crop((x,y,x+bounds['width'],y+bounds['height']))
            return SimpleNamespace(size=image.size,rgb=image.tobytes())
        finally:
            if previous: gdi.SelectObject(memory,previous)
            if bitmap: gdi.DeleteObject(bitmap)
            if memory: gdi.DeleteDC(memory)
            if dc: user.ReleaseDC(hwnd,dc)

    def __enter__(self): return self
    def __exit__(self,*args): pass


def run_capture(kind, params):
    from media_support import MediaError,HIDDEN
    try:
        result=subprocess.run([sys.executable,str(Path(__file__).resolve()),kind,json.dumps(params)],
            capture_output=True,text=True,timeout=params.get('seconds',0)+45,creationflags=HIDDEN)
    except subprocess.TimeoutExpired as exc:
        raise MediaError('Window capture timed out; the game may not support it.') from exc
    try: output=json.loads(result.stdout)
    except ValueError: raise MediaError('Window capture worker failed.')
    if result.returncode: raise MediaError(output.get('error','Window capture failed.'))
    return Path(output['path'])


if __name__=='__main__':
    import media_support as media
    try:
        params=json.loads(sys.argv[2]); params['_in_worker']=True
        path=(media.screenshot if sys.argv[1]=='screenshot' else media.record_clip)(**params)
        print(json.dumps({'path':str(path)}))
    except Exception as exc:
        print(json.dumps({'error':str(exc) if isinstance(exc,media.MediaError) else 'Window capture failed.'}))
        sys.exit(1)
