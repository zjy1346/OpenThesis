import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import './styles.css'
import { applyLanguageMetadata, resolveInitialLanguage } from './language'

applyLanguageMetadata(resolveInitialLanguage())

createRoot(document.getElementById('root')!).render(
  <StrictMode><App /></StrictMode>
)
