(() => {
  const sidebar = document.getElementById('adminSidebar');
  const openBtn = document.querySelector('[data-admin-sidebar-open]');
  const closeBtns = document.querySelectorAll('[data-admin-sidebar-close]');
  const overlay = document.querySelector('.admin-overlay');

  const openSidebar = () => {
    if (!sidebar) return;
    sidebar.classList.add('is-open');
    overlay?.classList.add('is-open');
  };

  const closeSidebar = () => {
    sidebar?.classList.remove('is-open');
    overlay?.classList.remove('is-open');
  };

  openBtn?.addEventListener('click', openSidebar);
  closeBtns.forEach((btn) => btn.addEventListener('click', closeSidebar));
})();

// Compress phone photos as soon as selected, before the publication request.
// Server-side validation/optimization also covers mirrors and older browsers.
(() => {
  const pending = new WeakMap();
  document.querySelectorAll('input[type="file"]').forEach(input => {
    input.addEventListener('change', () => {
      const work = (async () => {
        if (!window.createImageBitmap || !window.DataTransfer) return;
        const files = new DataTransfer();
        for (const file of input.files) {
          // GIF/WebP may be animated, branding/banners retain the original here.
          if (!/^image\/(jpeg|png)$/.test(file.type) || input.name !== 'featured_image_file') {
            files.items.add(file); continue;
          }
          let bitmap;
          try {
            bitmap = await createImageBitmap(file);
            if (bitmap.width * bitmap.height > 24000000) { files.items.add(file); continue; }
            const scale = Math.min(1, 1280 / Math.max(bitmap.width, bitmap.height));
            const canvas = document.createElement('canvas');
            canvas.width = Math.max(1, Math.round(bitmap.width * scale));
            canvas.height = Math.max(1, Math.round(bitmap.height * scale));
            canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
            const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/webp', .72));
            files.items.add(blob && blob.size < file.size ? new File([blob], file.name.replace(/\.[^.]+$/, '') + '.webp', {type:'image/webp'}) : file);
            canvas.width = canvas.height = 0;
          } catch { files.items.add(file); }
          finally { bitmap?.close(); }
        }
        input.files = files.files;
      })();
      pending.set(input, work);
    });
  });
  document.querySelectorAll('form[enctype="multipart/form-data"]').forEach(form => {
    let ready = false;
    form.addEventListener('submit', async event => {
      if (ready) return;
      const jobs = [...form.querySelectorAll('input[type="file"]')].map(input => pending.get(input)).filter(Boolean);
      if (!jobs.length) return;
      event.preventDefault();
      const button = event.submitter;
      await Promise.allSettled(jobs);
      ready = true;
      form.requestSubmit(button);
    });
  });
})();
