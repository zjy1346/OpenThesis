import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import OtPage from './OtPage'
import './styles.css'
import './ot.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode><OtPage /></StrictMode>
)
