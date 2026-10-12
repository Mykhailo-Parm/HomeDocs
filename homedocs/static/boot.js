// Тема до отрисовки страницы, чтобы не мигало
try {
  const th = localStorage.getItem('hd_theme');
  if (th === 'light' || th === 'dark') document.documentElement.dataset.theme = th;
} catch (e) { /* хранилище недоступно — системная тема */ }
