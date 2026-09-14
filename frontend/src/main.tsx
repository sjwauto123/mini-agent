/**
 * 前端入口：把 <App /> 挂载到 index.html 的 #root 节点上。
 * 开发期用 React.StrictMode 包裹，会对副作用做一次“双跑”检查，便于及早暴露问题。
 */
import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './styles.css'

ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>)
