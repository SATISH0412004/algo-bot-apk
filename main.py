"""Android entry point: runs the paper bot + web server inside the app and shows the dashboard in a WebView."""
import os
import threading

from kivy.app import App
from kivy.clock import Clock
from kivy.uix.label import Label

PORT = 8080


class AlgoBotApp(App):
    def build(self):
        return Label(text="Starting bot...")

    def on_start(self):
        from android.storage import app_storage_path
        os.chdir(app_storage_path())          # state + trades CSV are saved here
        os.environ.setdefault("BOT_PORT", str(PORT))

        import algo_bot_web as web
        from http.server import ThreadingHTTPServer
        threading.Thread(target=web.drain_queue, daemon=True).start()
        threading.Thread(target=web.bot.loop, daemon=True).start()
        server = ThreadingHTTPServer(("127.0.0.1", PORT), web.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        Clock.schedule_once(lambda dt: self.show_webview(), 1.5)

    def show_webview(self):
        from jnius import autoclass
        from android.runnable import run_on_ui_thread

        WebView = autoclass("android.webkit.WebView")
        WebViewClient = autoclass("android.webkit.WebViewClient")
        activity = autoclass("org.kivy.android.PythonActivity").mActivity

        @run_on_ui_thread
        def create():
            wv = WebView(activity)
            st = wv.getSettings()
            st.setJavaScriptEnabled(True)
            st.setDomStorageEnabled(True)
            wv.setWebViewClient(WebViewClient())
            activity.setContentView(wv)
            wv.loadUrl(f"http://127.0.0.1:{PORT}")

        create()


if __name__ == "__main__":
    AlgoBotApp().run()
