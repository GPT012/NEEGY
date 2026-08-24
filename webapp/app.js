(() => {
  "use strict";

  const tg = window.Telegram?.WebApp;

  const pageTitleEl = document.getElementById("page-title");
  const pageSubtitleEl = document.getElementById("page-subtitle");
  const photoListEl = document.getElementById("photo-list");
  const callListEl = document.getElementById("call-list");
  const errorBannerEl = document.getElementById("error-banner");
  const wheelStatusEl = document.getElementById("wheel-status-text");
  const wheelSpinBtn = document.getElementById("wheel-spin-btn");
  const vipContentEl = document.getElementById("vip-content");
  const tabButtons = document.querySelectorAll(".tab-btn");
  const views = {
    shop: document.getElementById("view-shop"),
    wheel: document.getElementById("view-wheel"),
    vip: document.getElementById("view-vip"),
  };
  const pageMeta = {
    shop: { title: "Nos services", subtitle: "Choisis ce qu'il te faut, ajoute-le au panier." },
    wheel: { title: "Roue quotidienne", subtitle: "Un tour gratuit par jour, tente ta chance !" },
    vip: { title: "Abonnement VIP", subtitle: "Accès exclusif et avantages chaque jour." },
  };

  /** @type {Array<object>} */
  let photos = [];
  /** @type {Array<object>} */
  let calls = [];
  /** @type {Map<number, {quantity:number, call_slot_id:number|null, call_slot_start_at:string|null}>} */
  let cartByProduct = new Map();
  let cartTotalCents = 0;
  let cartCurrency = "EUR";
  /** @type {Map<number, Array<{id:number,start_at:string,duration_minutes:number}>>} */
  const slotCache = new Map();
  /** @type {Set<number>} product_id dont le sélecteur de créneau est ouvert */
  const openSlotPickers = new Set();

  function initDataHeader() {
    return tg?.initData || "";
  }

  async function apiFetch(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-Telegram-Init-Data": initDataHeader(),
        ...(options.headers || {}),
      },
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.error || `Erreur ${response.status}`);
    }
    return response.json();
  }

  function formatPrice(cents, currency) {
    const symbol = currency === "EUR" ? "€" : currency;
    return `${(cents / 100).toFixed(2)} ${symbol}`;
  }

  function formatDateTime(isoString) {
    const date = new Date(isoString);
    return (
      date.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" }) +
      " à " +
      date.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit", timeZone: "UTC" }) +
      " UTC"
    );
  }

  function showError(message) {
    errorBannerEl.textContent = message;
    errorBannerEl.hidden = false;
    setTimeout(() => {
      errorBannerEl.hidden = true;
    }, 4000);
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  // ------------------------------------------------------------------
  // Navigation par onglets
  // ------------------------------------------------------------------

  function switchTab(tab) {
    for (const [name, el] of Object.entries(views)) {
      el.hidden = name !== tab;
    }
    for (const btn of tabButtons) {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    }
    pageTitleEl.textContent = pageMeta[tab].title;
    pageSubtitleEl.textContent = pageMeta[tab].subtitle;

    if (tab === "wheel") refreshWheelStatus();
    if (tab === "vip") refreshVipStatus();
    if (tab === "shop") updateMainButton();
  }

  for (const btn of tabButtons) {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  }

  // ------------------------------------------------------------------
  // Boutique : photos + appels
  // ------------------------------------------------------------------

  async function fetchCatalog() {
    const [photoResult, callResult, cart] = await Promise.all([
      apiFetch("/api/products?category=photo"),
      apiFetch("/api/products?category=call"),
      apiFetch("/api/cart"),
    ]);
    photos = photoResult;
    calls = callResult;
    applyCart(cart);
  }

  function applyCart(cart) {
    cartByProduct = new Map(
      cart.items.map((item) => [
        item.product_id,
        {
          quantity: item.quantity,
          call_slot_id: item.call_slot_id,
          call_slot_start_at: item.call_slot_start_at,
        },
      ])
    );
    cartTotalCents = cart.total_cents;
    cartCurrency = cart.items[0]?.currency || cartCurrency;
  }

  function renderShop() {
    renderPhotoList();
    renderCallList();
    updateMainButton();
  }

  function renderPhotoList() {
    if (!photos.length) {
      photoListEl.innerHTML = '<p class="loading">Aucune photo disponible pour le moment.</p>';
      return;
    }
    photoListEl.innerHTML = "";
    for (const product of photos) {
      photoListEl.appendChild(renderPhotoCard(product));
    }
  }

  function renderPhotoCard(product) {
    const entry = cartByProduct.get(product.id);
    const quantity = entry?.quantity || 0;

    const card = document.createElement("div");
    card.className = "product-card";

    const info = document.createElement("div");
    info.className = "product-info";
    info.innerHTML = `
      <p class="product-name">${escapeHtml(product.name)}</p>
      <p class="product-description">${escapeHtml(product.description)}</p>
      <p class="product-price">${formatPrice(product.price_cents, product.currency)}</p>
    `;
    card.appendChild(info);

    if (quantity > 0) {
      const control = document.createElement("div");
      control.className = "qty-control";

      const minusBtn = document.createElement("button");
      minusBtn.className = "qty-btn";
      minusBtn.textContent = "−";
      minusBtn.addEventListener("click", () => changeQuantity(product.id, quantity - 1));

      const value = document.createElement("span");
      value.className = "qty-value";
      value.textContent = String(quantity);

      const plusBtn = document.createElement("button");
      plusBtn.className = "qty-btn";
      plusBtn.textContent = "+";
      plusBtn.addEventListener("click", () => changeQuantity(product.id, quantity + 1));

      control.append(minusBtn, value, plusBtn);
      card.appendChild(control);
    } else {
      const addBtn = document.createElement("button");
      addBtn.className = "add-btn";
      addBtn.textContent = "Ajouter";
      addBtn.addEventListener("click", () => changeQuantity(product.id, 1));
      card.appendChild(addBtn);
    }

    return card;
  }

  function renderCallList() {
    if (!calls.length) {
      callListEl.innerHTML = '<p class="loading">Aucun appel disponible pour le moment.</p>';
      return;
    }
    callListEl.innerHTML = "";
    for (const product of calls) {
      callListEl.appendChild(renderCallCard(product));
    }
  }

  function renderCallCard(product) {
    const entry = cartByProduct.get(product.id);

    const card = document.createElement("div");
    card.className = "product-card call-card";

    const info = document.createElement("div");
    info.className = "product-info";
    info.innerHTML = `
      <p class="product-name">${escapeHtml(product.name)}</p>
      <p class="product-description">${escapeHtml(product.description)}</p>
      <p class="product-price">${formatPrice(product.price_cents, product.currency)} · ${product.duration_minutes} min</p>
    `;
    card.appendChild(info);

    if (entry?.call_slot_id) {
      const booked = document.createElement("div");
      booked.className = "slot-booked";
      booked.innerHTML = `<span>✅ ${escapeHtml(formatDateTime(entry.call_slot_start_at))}</span>`;
      const removeBtn = document.createElement("button");
      removeBtn.className = "remove-btn";
      removeBtn.textContent = "Retirer";
      removeBtn.addEventListener("click", () => removeCartItem(product.id));
      booked.appendChild(removeBtn);
      card.appendChild(booked);
      return card;
    }

    const chooseBtn = document.createElement("button");
    chooseBtn.className = "add-btn";
    chooseBtn.textContent = openSlotPickers.has(product.id) ? "Masquer les créneaux" : "Choisir un créneau";
    chooseBtn.addEventListener("click", () => toggleSlotPicker(product));
    card.appendChild(chooseBtn);

    if (openSlotPickers.has(product.id)) {
      const picker = document.createElement("div");
      picker.className = "slot-picker";
      const cached = slotCache.get(product.id);
      if (!cached) {
        picker.innerHTML = '<p class="loading">Chargement des créneaux…</p>';
      } else if (!cached.length) {
        picker.innerHTML = '<p class="loading">Aucun créneau disponible pour le moment.</p>';
      } else {
        for (const slot of cached) {
          const chip = document.createElement("button");
          chip.className = "slot-chip";
          chip.textContent = formatDateTime(slot.start_at);
          chip.addEventListener("click", () => bookSlot(product, slot));
          picker.appendChild(chip);
        }
      }
      card.appendChild(picker);
    }

    return card;
  }

  async function toggleSlotPicker(product) {
    if (openSlotPickers.has(product.id)) {
      openSlotPickers.delete(product.id);
      renderCallList();
      return;
    }
    openSlotPickers.add(product.id);
    renderCallList();

    if (!slotCache.has(product.id)) {
      try {
        const slots = await apiFetch(`/api/call-slots?duration=${product.duration_minutes}`);
        slotCache.set(product.id, slots);
      } catch (err) {
        slotCache.set(product.id, []);
        showError(err.message);
      }
      renderCallList();
    }
  }

  async function bookSlot(product, slot) {
    try {
      const cart = await apiFetch("/api/cart", {
        method: "POST",
        body: JSON.stringify({ product_id: product.id, quantity: 1, call_slot_id: slot.id }),
      });
      applyCart(cart);
      openSlotPickers.delete(product.id);
      slotCache.delete(product.id);
      renderShop();
      tg?.HapticFeedback?.impactOccurred("light");
    } catch (err) {
      showError(err.message);
    }
  }

  async function changeQuantity(productId, newQuantity) {
    try {
      const cart = await apiFetch("/api/cart", {
        method: "POST",
        body: JSON.stringify({ product_id: productId, quantity: newQuantity }),
      });
      applyCart(cart);
      renderShop();
      tg?.HapticFeedback?.impactOccurred("light");
    } catch (err) {
      showError(err.message);
    }
  }

  async function removeCartItem(productId) {
    try {
      const cart = await apiFetch(`/api/cart/${productId}`, { method: "DELETE" });
      applyCart(cart);
      renderShop();
      tg?.HapticFeedback?.impactOccurred("light");
    } catch (err) {
      showError(err.message);
    }
  }

  function updateMainButton() {
    if (!tg) return;
    if (views.shop.hidden) {
      tg.MainButton.hide();
      return;
    }
    if (cartTotalCents > 0) {
      tg.MainButton.setText(`Commander • ${formatPrice(cartTotalCents, cartCurrency)}`);
      tg.MainButton.show();
    } else {
      tg.MainButton.hide();
    }
  }

  async function handleCheckout() {
    if (!tg) return;
    tg.MainButton.showProgress(true);
    try {
      const result = await apiFetch("/api/checkout", { method: "POST" });
      cartByProduct = new Map();
      cartTotalCents = 0;
      renderShop();
      tg.HapticFeedback?.notificationOccurred("success");
      const discountLine = result.discount_percent
        ? `\nRéduction roue appliquée : -${result.discount_percent}%`
        : "";
      tg.showPopup(
        {
          title: "Commande confirmée",
          message: `Commande #${result.order_id} enregistrée pour ${formatPrice(
            result.total_cents,
            result.currency
          )}.${discountLine}\nLe récapitulatif t'a été envoyé dans le chat.`,
          buttons: [{ type: "close" }],
        },
        () => tg.close()
      );
    } catch (err) {
      tg.HapticFeedback?.notificationOccurred("error");
      showError(err.message);
    } finally {
      tg.MainButton.hideProgress();
    }
  }

  // ------------------------------------------------------------------
  // Roue quotidienne
  // ------------------------------------------------------------------

  async function refreshWheelStatus() {
    wheelSpinBtn.disabled = true;
    try {
      const status = await apiFetch("/api/wheel/status");
      if (status.can_spin) {
        wheelStatusEl.textContent = "Un tour gratuit t'attend aujourd'hui !";
        wheelSpinBtn.disabled = false;
        wheelSpinBtn.textContent = "Tourner la roue";
      } else {
        const prizeLabel = status.prize ? status.prize.label : "un lot";
        wheelStatusEl.textContent = `Tu as déjà gagné aujourd'hui : ${prizeLabel}. Reviens demain !`;
        wheelSpinBtn.disabled = true;
        wheelSpinBtn.textContent = "Déjà joué aujourd'hui";
      }
    } catch (err) {
      wheelStatusEl.textContent = "Impossible de charger la roue pour le moment.";
      showError(err.message);
    }
  }

  wheelSpinBtn.addEventListener("click", async () => {
    wheelSpinBtn.disabled = true;
    try {
      const prize = await apiFetch("/api/wheel/spin", { method: "POST" });
      tg?.HapticFeedback?.notificationOccurred("success");
      if (tg) {
        tg.showPopup({
          title: "🎉 Gagné !",
          message: `${prize.label}\n${prize.description}`,
          buttons: [{ type: "close" }],
        });
      } else {
        window.alert(`${prize.label}\n${prize.description}`);
      }
      await refreshWheelStatus();
    } catch (err) {
      showError(err.message);
      await refreshWheelStatus();
    }
  });

  // ------------------------------------------------------------------
  // VIP
  // ------------------------------------------------------------------

  async function refreshVipStatus() {
    vipContentEl.innerHTML = '<p class="loading">Chargement…</p>';
    try {
      const [status, vipProducts] = await Promise.all([
        apiFetch("/api/vip/status"),
        apiFetch("/api/products?category=vip"),
      ]);
      renderVip(status, vipProducts);
    } catch (err) {
      vipContentEl.innerHTML = '<p class="loading">Impossible de charger le statut VIP.</p>';
      showError(err.message);
    }
  }

  function renderVip(status, vipProducts) {
    vipContentEl.innerHTML = "";

    if (status.active) {
      const activeCard = document.createElement("div");
      activeCard.className = "vip-card vip-active";
      const expires = status.expires_at ? formatDateTime(status.expires_at) : "";
      activeCard.innerHTML = `
        <p class="vip-badge">⭐ VIP actif</p>
        <p class="vip-plan-name">${escapeHtml(status.plan_name || "")}</p>
        <p class="vip-expiry">Actif jusqu'au ${escapeHtml(expires)}</p>
      `;
      vipContentEl.appendChild(activeCard);
      return;
    }

    if (!vipProducts.length) {
      vipContentEl.innerHTML = '<p class="loading">Aucune formule VIP disponible pour le moment.</p>';
      return;
    }

    for (const product of vipProducts) {
      const entry = cartByProduct.get(product.id);
      const card = document.createElement("div");
      card.className = "vip-card";
      card.innerHTML = `
        <p class="vip-plan-name">${escapeHtml(product.name)}</p>
        <p class="product-description">${escapeHtml(product.description)}</p>
        <p class="product-price">${formatPrice(product.price_cents, product.currency)}</p>
      `;

      const button = document.createElement("button");
      if (entry?.quantity) {
        button.className = "remove-btn";
        button.textContent = "Retirer du panier";
        button.addEventListener("click", async () => {
          await removeCartItem(product.id);
          await refreshVipStatus();
        });
      } else {
        button.className = "add-btn";
        button.textContent = "S'abonner (ajouter au panier)";
        button.addEventListener("click", async () => {
          await changeQuantity(product.id, 1);
          await refreshVipStatus();
        });
      }
      card.appendChild(button);
      vipContentEl.appendChild(card);
    }

    const hint = document.createElement("p");
    hint.className = "vip-hint";
    hint.textContent =
      "Valide ensuite ta commande depuis l'onglet Boutique (bouton en bas de l'écran).";
    vipContentEl.appendChild(hint);
  }

  // ------------------------------------------------------------------
  // Démarrage
  // ------------------------------------------------------------------

  async function init() {
    if (tg) {
      tg.ready();
      tg.expand();
      tg.MainButton.onClick(handleCheckout);
    }

    switchTab("shop");

    try {
      await fetchCatalog();
      renderShop();
    } catch (err) {
      showError(err.message);
      photoListEl.innerHTML = '<p class="loading">Impossible de charger le catalogue.</p>';
      callListEl.innerHTML = "";
    }
  }

  init();
})();
