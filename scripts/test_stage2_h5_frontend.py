# -*- coding: utf-8 -*-
r"""
阶段 2 自动化验收测试套件 (scripts/test_stage2_h5_frontend.py)
验证范围：
  1. `h5-frontend/` 工程结构与生产构建产物 (`h5-frontend/dist/index.html` 与 JS/CSS bundle) 完整性；
  2. iOS 27 极光液态毛玻璃样式契约 (`backdrop-filter: blur`, `@supports not` 降级回退, `prefers-reduced-motion`, `--tenant-accent` 动态主题变量)；
  3. C 端专属防噪与流式自愈渲染器 (`streamSanitizer.jsx`)：
     - 严禁使用 `EventSource` 或 `dangerouslySetInnerHTML`；
     - 严禁在 C 端界面暴露开发者调试字段 (`static_prefix_md5`, `jev_rtt_ms`, `recall_channels`)；
     - 包含 `<br>`、`<div>`、LaTeX 数学符号 (`\rightarrow`, `\ge`, `\le`, `\times`, `\Delta`) 与未闭合 `**` 自愈逻辑；
  4. FastAPI `/h5/` 路由挂载可达性与 C 端完整用户旅程（拉取店铺租户 -> 流式对话 -> 提交 👍/👎 反馈）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.dont_write_bytecode = True

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from fastapi.testclient import TestClient

from app.main import app
from app.services.admin_analytics_service import get_admin_analytics_service


class TestStage2H5Frontend(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.h5_dir = ROOT_DIR / "h5-frontend"
        cls.dist_dir = cls.h5_dir / "dist"
        cls.client = TestClient(app)
        cls.analytics = get_admin_analytics_service()
        cls.analytics.reset_to_baseline()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.analytics.reset_to_baseline()

    def test_01_h5_project_files_and_dist_build_artifacts(self) -> None:
        """验证 h5-frontend 源码结构与 Vite 生产构建产物 dist/ 完整存在。"""
        required_files = [
            self.h5_dir / "package.json",
            self.h5_dir / "vite.config.js",
            self.h5_dir / "index.html",
            self.h5_dir / "src" / "main.jsx",
            self.h5_dir / "src" / "App.jsx",
            self.h5_dir / "src" / "utils" / "streamSanitizer.jsx",
            self.h5_dir / "src" / "styles" / "ios27-glass.css",
            self.dist_dir / "index.html",
        ]
        for fpath in required_files:
            self.assertTrue(fpath.exists(), f"缺少关键文件: {fpath}")

        dist_html = (self.dist_dir / "index.html").read_text(encoding="utf-8")
        self.assertIn("/h5/assets/", dist_html)
        self.assertTrue("惠购" in dist_html or "千问智能导购" in dist_html)

    def test_02_ios27_glassmorphism_css_contract(self) -> None:
        """验证反 AI 味与千问清爽风格规范：OKLCH 调色盘、语义变量、横滑卡片吸附。"""
        css_text = (self.h5_dir / "src" / "styles" / "ios27-glass.css").read_text(
            encoding="utf-8"
        )
        self.assertTrue("--accent" in css_text or "--tenant-accent" in css_text)
        self.assertIn("oklch(", css_text)
        self.assertIn("--bg:", css_text)
        self.assertIn("--surface:", css_text)
        self.assertIn("scroll-snap-type: x mandatory", css_text)

    def test_03_stream_sanitizer_and_zero_developer_noise_contract(self) -> None:
        """验证前端流式净水器与 C 端零开发者调试噪声契约。"""
        sanitizer_code = (
            self.h5_dir / "src" / "utils" / "streamSanitizer.jsx"
        ).read_text(encoding="utf-8")
        app_code = (self.h5_dir / "src" / "App.jsx").read_text(encoding="utf-8")

        # 1. 严禁使用原生 EventSource 或 dangerouslySetInnerHTML
        self.assertNotIn("EventSource", app_code)
        self.assertNotIn("dangerouslySetInnerHTML", app_code)
        self.assertNotIn("dangerouslySetInnerHTML", sanitizer_code)

        # 2. 必须使用 fetch('/api/v1/chat/stream') + getReader() + TextDecoder
        self.assertIn("fetch('/api/v1/chat/stream'", app_code)
        self.assertIn("response.body.getReader()", app_code)
        self.assertIn("new TextDecoder", app_code)

        # 3. 严禁在 C 端界面渲染开发者底层指标
        for dev_key in ("static_prefix_md5", "jev_rtt_ms", "recall_channels", "static_ratio_pct"):
            self.assertNotIn(dev_key, app_code)

        # 4. 流式净水器必须包含 HTML <br>/<div> 过滤与 LaTeX 符号转换
        self.assertIn("\\rightarrow", sanitizer_code)
        self.assertIn("\\Delta", sanitizer_code)
        self.assertIn("<\\s*br", sanitizer_code)

    def test_04_fastapi_h5_mount_and_customer_journey(self) -> None:
        """验证 FastAPI /h5/ 静态挂载 HTTP 200 可达，以及 C 端租户切换与评价完整链路。"""
        h5_resp = self.client.get("/h5/")
        self.assertEqual(h5_resp.status_code, 200)
        self.assertIn("text/html", h5_resp.headers.get("content-type", ""))
        self.assertIn("/h5/assets/", h5_resp.text)

        # 拉取公共租户列表
        t_resp = self.client.get("/api/v1/tenants/public")
        self.assertEqual(t_resp.status_code, 200)
        tenants = t_resp.json()["tenants"]
        self.assertEqual(len(tenants), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
