(() => {
  const stage = document.getElementById('authStage');
  if (!stage) return;

  const shapeMessage = document.getElementById('shapeMessage');
  const greeting = document.getElementById('authGreeting');
  const faces = {
    login: stage.querySelector('.auth-face-login'),
    register: stage.querySelector('.auth-face-register'),
  };
  let passwordBlurTimer = null;

  const modeCopy = {
    login: {
      message: '我们正在安静等候',
      title: '登录',
      path: '/login',
    },
    register: {
      message: '准备认识一位新用户',
      title: '注册',
      path: '/register',
    },
  };

  function setMode(mode, updateHistory = false) {
    const nextMode = mode === 'register' ? 'register' : 'login';
    stage.classList.toggle('is-register', nextMode === 'register');
    stage.dataset.currentMode = nextMode;
    faces.login?.setAttribute('aria-hidden', String(nextMode !== 'login'));
    faces.register?.setAttribute('aria-hidden', String(nextMode !== 'register'));
    if (shapeMessage) shapeMessage.textContent = modeCopy[nextMode].message;
    document.title = `${modeCopy[nextMode].title} · 缺陷检测GUI`;

    if (updateHistory && window.location.pathname !== modeCopy[nextMode].path) {
      window.history.pushState({ authMode: nextMode }, '', modeCopy[nextMode].path);
    }
  }

  function updateGreeting() {
    if (!greeting) return;
    const hour = new Date().getHours();
    let text = '晚上好，欢迎回来';
    if (hour < 6) text = '夜深了，辛苦啦';
    else if (hour < 11) text = '早上好，今天也要顺利';
    else if (hour < 14) text = '中午好，欢迎回来';
    else if (hour < 18) text = '下午好，欢迎回来';
    greeting.textContent = text;
  }

  function startPasswordReaction() {
    window.clearTimeout(passwordBlurTimer);
    stage.classList.remove('is-watching');
    stage.classList.add('password-active');
    if (shapeMessage) shapeMessage.textContent = '密码时间，我们向左躲一躲';
  }

  function stopPasswordReaction() {
    window.clearTimeout(passwordBlurTimer);
    passwordBlurTimer = window.setTimeout(() => {
      if (stage.querySelector('[data-password-input]:focus')) return;
      stage.classList.remove('password-active', 'password-revealed');
      if (shapeMessage) shapeMessage.textContent = modeCopy[stage.dataset.currentMode || 'login'].message;
    }, 180);
  }

  stage.querySelectorAll('[data-auth-mode]').forEach((button) => {
    button.addEventListener('click', () => {
      setMode(button.dataset.authMode, true);
      stage.classList.remove('password-active', 'password-revealed', 'is-watching');
      window.setTimeout(() => {
        const activeFace = faces[stage.dataset.currentMode];
        activeFace?.querySelector('input[name="username"]')?.focus({ preventScroll: true });
      }, 520);
    });
  });

  stage.querySelectorAll('input[name="username"]').forEach((input) => {
    input.addEventListener('focus', () => {
      stage.classList.add('is-watching');
      stage.style.setProperty('--eye-x', '4px');
      stage.style.setProperty('--eye-y', '1px');
      if (shapeMessage) shapeMessage.textContent = '正在确认你的账号';
    });
    input.addEventListener('input', () => {
      stage.classList.add('is-watching');
      stage.style.setProperty('--eye-x', '4px');
      stage.style.setProperty('--eye-y', '1px');
    });
    input.addEventListener('blur', () => {
      stage.classList.remove('is-watching');
      stage.style.setProperty('--eye-x', '0px');
      stage.style.setProperty('--eye-y', '0px');
      if (!stage.classList.contains('password-active') && shapeMessage) {
        shapeMessage.textContent = modeCopy[stage.dataset.currentMode || 'login'].message;
      }
    });
  });

  stage.querySelectorAll('[data-password-input]').forEach((input) => {
    input.addEventListener('focus', startPasswordReaction);
    input.addEventListener('blur', stopPasswordReaction);
  });

  stage.querySelectorAll('[data-password-toggle]').forEach((button) => {
    button.addEventListener('click', () => {
      const input = button.parentElement?.querySelector('[data-password-input]');
      if (!input) return;
      const visible = input.type === 'text';
      input.type = visible ? 'password' : 'text';
      button.classList.toggle('is-visible', !visible);
      button.setAttribute('aria-label', visible ? '显示密码' : '隐藏密码');
      stage.classList.toggle('password-revealed', !visible);
      input.focus({ preventScroll: true });
    });
  });

  stage.querySelectorAll('[data-forgot-password]').forEach((button) => {
    button.addEventListener('click', () => {
      showToast('请联系管理员重置密码', 'secondary');
    });
  });

  const registerForm = document.getElementById('registerForm');
  if (registerForm) {
    const password = registerForm.querySelector('input[name="password"]');
    const confirmation = registerForm.querySelector('input[name="confirm_password"]');
    const validateConfirmation = () => {
      confirmation.setCustomValidity(
        confirmation.value && confirmation.value !== password.value
          ? '两次输入的密码不一致'
          : '',
      );
    };
    password?.addEventListener('input', validateConfirmation);
    confirmation?.addEventListener('input', validateConfirmation);
  }

  stage.querySelectorAll('.auth-form').forEach((form) => {
    form.addEventListener('submit', () => {
      if (form.checkValidity()) form.classList.add('is-submitting');
    });
  });

  window.addEventListener('popstate', () => {
    setMode(window.location.pathname.startsWith('/register') ? 'register' : 'login');
  });

  updateGreeting();
  setMode(stage.dataset.initialMode || 'login');
})();
