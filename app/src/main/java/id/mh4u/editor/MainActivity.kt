package id.mh4u.editor

import android.annotation.SuppressLint
import android.net.Uri
import android.os.Bundle
import android.util.Base64
import android.view.View
import android.webkit.JavascriptInterface
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.ProgressBar
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import kotlin.concurrent.thread

class MainActivity : AppCompatActivity() {

    private lateinit var web: WebView
    private lateinit var loading: ProgressBar
    private var chooserCallback: ValueCallback<Array<Uri>>? = null
    private var pendingBytes: ByteArray? = null

    // Pemilih file untuk "Muat save" (input type=file di halaman)
    private val pickFile = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { r ->
        val cb = chooserCallback
        chooserCallback = null
        cb?.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(r.resultCode, r.data))
    }

    // Penyimpanan hasil edit (tombol "Unduh") lewat Storage Access Framework
    private val createDoc = registerForActivityResult(ActivityResultContracts.CreateDocument("*/*")) { uri ->
        val data = pendingBytes
        pendingBytes = null
        if (uri == null || data == null) return@registerForActivityResult
        try {
            contentResolver.openOutputStream(uri, "wt")!!.use { it.write(data) }
            toast("Tersimpan (${data.size} byte)")
        } catch (e: Exception) {
            toast("Gagal menyimpan: ${e.message}")
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        web = WebView(this).apply {
            setBackgroundColor(getColor(R.color.bg))
            visibility = View.INVISIBLE
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.allowFileAccess = false
            addJavascriptInterface(Bridge(), "Android")
            webViewClient = WebViewClient()
            webChromeClient = object : WebChromeClient() {
                override fun onShowFileChooser(
                    view: WebView, cb: ValueCallback<Array<Uri>>, params: FileChooserParams
                ): Boolean {
                    chooserCallback?.onReceiveValue(null)
                    chooserCallback = cb
                    return try {
                        pickFile.launch(params.createIntent())
                        true
                    } catch (e: Exception) {
                        chooserCallback = null
                        false
                    }
                }
            }
        }
        loading = ProgressBar(this)
        setContentView(FrameLayout(this).apply {
            addView(web, FrameLayout.LayoutParams(-1, -1))
            addView(loading, FrameLayout.LayoutParams(-2, -2, android.view.Gravity.CENTER))
        })

        startServer()
    }

    private fun startServer() {
        thread {
            try {
                val iconDir = copyIcons()
                if (!Python.isStarted()) Python.start(AndroidPlatform(this))
                val port = Python.getInstance().getModule("android_main")
                    .callAttr("start", iconDir.absolutePath).toInt()
                runOnUiThread {
                    web.loadUrl("http://127.0.0.1:$port/")
                    web.visibility = View.VISIBLE
                    loading.visibility = View.GONE
                }
            } catch (e: Throwable) {
                runOnUiThread { toast("Gagal memulai server: ${e.message}") }
            }
        }
    }

    /** Salin assets/icon -> filesDir/icon (sekali per versi), karena server Python membaca ikon dari folder biasa. */
    private fun copyIcons(): File {
        val dir = File(filesDir, "icon")
        val marker = File(filesDir, "icon.ver")
        val ver = packageManager.getPackageInfo(packageName, 0).let {
            @Suppress("DEPRECATION") it.versionName + "/" + it.lastUpdateTime
        }
        if (dir.isDirectory && marker.exists() && marker.readText() == ver) return dir
        dir.mkdirs()
        for (name in assets.list("icon").orEmpty()) {
            assets.open("icon/$name").use { i -> File(dir, name).outputStream().use { o -> i.copyTo(o) } }
        }
        marker.writeText(ver)
        return dir
    }

    private fun toast(s: String) = Toast.makeText(this, s, Toast.LENGTH_LONG).show()

    inner class Bridge {
        @JavascriptInterface
        fun saveFile(b64: String, name: String) {
            pendingBytes = Base64.decode(b64, Base64.DEFAULT)
            runOnUiThread { createDoc.launch(name.ifBlank { "user1" }) }
        }
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else @Suppress("DEPRECATION") super.onBackPressed()
    }
}
