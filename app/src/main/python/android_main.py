# -*- coding: utf-8 -*-
"""Jembatan Android -> mh4u_server.py (tanpa mengubah file server asli)."""
import threading
import mh4u_server as S

_srv = None
_port = 0
_lock = threading.Lock()

# Di WebView, unduhan blob: tidak jalan. Jika ada jembatan Android, kirim byte ke Kotlin.
_OLD = "a.href=URL.createObjectURL(b);a.download=fname;a.click();"
_NEW = ("if(window.Android&&Android.saveFile){const rd=new FileReader();"
        "rd.onloadend=()=>Android.saveFile(rd.result.split(',')[1],fname);rd.readAsDataURL(b)}"
        "else{a.href=URL.createObjectURL(b);a.download=fname;a.click();}")


def start(icon_dir):
    """Mulai server di 127.0.0.1 pada port bebas; kembalikan port. Aman dipanggil berulang."""
    global _srv, _port
    with _lock:
        if _srv is not None:
            return _port
        S.ICON_DIR = icon_dir
        S.NAMES[:] = S.parse_names(S.NAMES_DATA)
        S.scan_icons()
        S.build_meta()
        if _OLD in S.PAGE:
            S.PAGE = S.PAGE.replace(_OLD, _NEW)
        else:
            print("PERINGATAN: potongan JS unduhan tidak ditemukan; tombol Unduh mungkin tidak jalan")
        _srv = S.ThreadingHTTPServer(("127.0.0.1", 0), S.H)
        _port = _srv.server_address[1]
        threading.Thread(target=_srv.serve_forever, daemon=True).start()
        return _port
