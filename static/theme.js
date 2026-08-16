function toggleTheme() {
    const current = document.documentElement.getAttribute('data-theme') || 'dark';
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('theme', next); } catch (e) {}
    updateThemeToggleIcon();
}

function updateThemeToggleIcon() {
    const btn = document.getElementById('theme-toggle-btn');
    if (!btn) return;
    const isLight = document.documentElement.getAttribute('data-theme') === 'light';
    btn.innerHTML = isLight ? '<i class="ph-bold ph-moon"></i>' : '<i class="ph-bold ph-sun"></i>';
    btn.title = isLight ? 'Mudar para tema escuro' : 'Mudar para tema claro';
}

document.addEventListener('DOMContentLoaded', updateThemeToggleIcon);
