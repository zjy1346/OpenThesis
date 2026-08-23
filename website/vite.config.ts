import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: '/',
  build: { target: 'es2022', rollupOptions: { input: { main: 'index.html', ot: 'ot/index.html' } } }
})
