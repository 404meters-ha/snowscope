import { fileURLToPath } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

// BASE_PATH 与后端共用项目根的 .env（子路径部署，如 /celestial-snow/），环境变量可临时覆盖。
// 设定后构建产物引用 {BASE}/assets/…，dev 页面与代理也统一挂到该前缀下。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, fileURLToPath(new URL('..', import.meta.url)), '')
  const base = (process.env.BASE_PATH || env.BASE_PATH || '').replace(/\/+$/, '')

  return {
    base: base ? `${base}/` : '/',
    plugins: [vue()],
    server: {
      // 前缀原样透传给后端，由后端 BASE_PATH 中间件剥掉（与 nginx 透传同一约定）
      proxy: Object.fromEntries(
        ['/api', '/courses'].map((p) => [base + p, { target: 'http://localhost:8100' }]),
      ),
    },
    build: {
      outDir: 'dist',
      chunkSizeWarningLimit: 2000,
    },
  }
})
