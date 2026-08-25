(() => {
  "use strict";

  const tg = window.Telegram?.WebApp;

  const pageTitleEl = document.getElementById("page-title");
  const pageSubtitleEl = document.getElementById("page-subtitle");
  const photoListEl = document.getElementById("photo-list");
  const photoNoteEl = document.getElementById("photo-note");
  const callListEl = document.getElementById("call-list");
  const toastEl = document.getElementById("toast");
  const paySheetEl = document.getElementById("pay-sheet");
  const payHeadingEl = document.getElementById("pay-heading");
  const payTotalEl = document.getElementById("pay-total");
  const payPaypalEl = document.getElementById("pay-paypal");
  const payCryptoOpenEl = document.getElementById("pay-crypto-open");
  const cryptoSheetEl = document.getElementById("crypto-sheet");
  const cryptoListEl = document.getElementById("crypto-list");
  const cryptoBackEl = document.getElementById("crypto-back");
  const payBankEl = document.getElementById("pay-bank");
  const payHolderEl = document.getElementById("pay-holder");
  const payIbanCopyEl = document.getElementById("pay-iban-copy");
  const payRefEl = document.getElementById("pay-ref");
  const payDoneEl = document.getElementById("pay-done");
  const payPointsEl = document.getElementById("pay-points");
  const payPointsHintEl = document.getElementById("pay-points-hint");
  const pointsBalanceEl = document.getElementById("wheel-hub");
  const cartStripEl = document.getElementById("cart-strip");
  const shopBadgeEl = document.getElementById("shop-badge");
  const wheelStatusEl = document.getElementById("wheel-status-text");
  const wheelSpinBtn = document.getElementById("wheel-spin-btn");
  const wheelGraphicEl = document.getElementById("wheel-graphic");
  const vipContentEl = document.getElementById("vip-content");
  const tabButtons = document.querySelectorAll(".tab-btn");

  const views = {
    shop: document.getElementById("view-shop"),
    wheel: document.getElementById("view-wheel"),
    vip: document.getElementById("view-vip"),
  };
  const pageMeta = {
    shop: { title: "Le deck", subtitle: "Choisis ton booster. Chaque pack a sa rareté." },
    wheel: { title: "Les roues", subtitle: "Une quotidienne. Deux payantes." },
    vip: { title: "Le cercle", subtitle: "L'accès discret, sans ostentation." },
  };
  const BOOSTER_TIERS = [
    { rarity: "COMMON", energy: "feuille", cards: "3 cartes" },
    { rarity: "UNCOMMON", energy: "vague", cards: "8 cartes" },
    { rarity: "RARE", energy: "éclair", cards: "12 cartes" },
  ];
  const CALL_TIERS = {
    15: { rarity: "FIRE", energy: "feu" },
    30: { rarity: "PSY", energy: "esprit" },
  };

  const wheelSwitchEl = document.getElementById("wheel-switch");
  const SPIN_DURATION_MS = 3400;
  /** @type {Array<object>} */
  let wheelsCatalog = [];
  let selectedWheelSlug = "free";

  function currentWheel() {
    return wheelsCatalog.find((wheel) => wheel.slug === selectedWheelSlug) || wheelsCatalog[0];
  }

  function currentSlices() {
    const wheel = currentWheel();
    if (wheel?.slices?.length) return wheel.slices;
    return [
      { label: "2", kind: "points" },
      { label: "4", kind: "points" },
      { label: "2", kind: "points" },
      { label: "4", kind: "points" },
      { label: "2", kind: "points" },
      { label: "9", kind: "points" },
      { label: "2", kind: "points" },
      { label: "4", kind: "points" },
      { label: "2", kind: "points" },
      { label: "4", kind: "points" },
    ];
  }

  function paintWheel() {
    if (!wheelGraphicEl) return;
    const slices = currentSlices();
    wheelGraphicEl.innerHTML = "";
    slices.forEach((slice, index) => {
      const face = document.createElement("span");
      const classes = ["wheel-face"];
      if (slice.kind === "video" || slice.label === "9" || slice.label === "36") {
        classes.push("wheel-face-rare");
      }
      const isLight = index % 2 === 1;
      if (isLight && slice.kind === "points") classes.push("wheel-face-ink");
      face.className = classes.join(" ");
      const deg = index * (360 / slices.length) + 180 / slices.length;
      face.style.transform = `rotate(${deg}deg) translateY(-82px)`;
      face.textContent = slice.label === "photo" ? "◆" : slice.label === "vidéo" ? "▶" : slice.label === "cam" ? "☎" : slice.label;
      wheelGraphicEl.appendChild(face);
    });
  }

  function sliceIndexForPrize(prize) {
    const slices = currentSlices();
    const matches = [];
    slices.forEach((slice, index) => {
      if (prize.kind && slice.kind === prize.kind) {
        if (prize.kind === "points" && String(slice.label) === String(prize.points_amount || prize.label)) {
          matches.push(index);
        } else if (prize.kind !== "points") {
          matches.push(index);
        }
      } else if (String(slice.label) === String(prize.label || prize.points_amount)) {
        matches.push(index);
      }
    });
    if (!matches.length) return 0;
    return matches[Math.floor(Math.random() * matches.length)];
  }

  function animateSpinToPrize(prize) {
    const slices = currentSlices();
    const step = 360 / Math.max(slices.length, 1);
    const index = sliceIndexForPrize(prize);
    const jitter = (Math.random() - 0.5) * Math.min(16, step / 2);
    const target = (360 - (index * step + step / 2) + jitter + 360) % 360;
    const current = ((wheelRotation % 360) + 360) % 360;
    let delta = (target - current + 360) % 360;
    if (delta < 80) delta += 360;
    wheelRotation += 4 * 360 + delta;
    wheelGraphicEl.style.transform = `rotate(${wheelRotation}deg)`;
  }

  /** @type {Array<object>} */
  let photos = [];
  /** @type {Array<object>} */
  let calls = [];
  /** @type {Map<number, {quantity:number, call_slot_id:number|null, call_slot_start_at:string|null}>} */
  let cartByProduct = new Map();
  let cartTotalCents = 0;
  let cartCurrency = "EUR";
  /** @type {object|null} */
  let pendingOrder = null;
  /** @type {Map<number, Array<{id:number,start_at:string,duration_minutes:number}>>} */
  const slotCache = new Map();
  /** @type {Set<number>} product_id dont le sélecteur de créneau est ouvert */
  const openSlotPickers = new Set();
  let wheelRotation = 0;
  let toastTimer = null;
  let lastPayOrder = null;

  // ------------------------------------------------------------------
  // Utilitaires
  // ------------------------------------------------------------------

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
    const amount = cents % 100 === 0 ? String(cents / 100) : (cents / 100).toFixed(2);
    return `${amount} ${symbol}`;
  }

  // Les créneaux sont affichés en UTC pour rester cohérents avec les
  // récapitulatifs envoyés dans le chat par le bot.
  function formatDateTime(isoString) {
    const date = new Date(isoString);
    const day = date.toLocaleDateString("fr-FR", {
      weekday: "short",
      day: "numeric",
      month: "short",
      timeZone: "UTC",
    });
    const time = date.toLocaleTimeString("fr-FR", {
      hour: "2-digit",
      minute: "2-digit",
      timeZone: "UTC",
    });
    return `${day} · ${time} UTC`;
  }

  function formatDate(isoString) {
    return new Date(isoString).toLocaleDateString("fr-FR", {
      day: "numeric",
      month: "long",
      year: "numeric",
      timeZone: "UTC",
    });
  }

  function showToast(message, variant) {
    toastEl.textContent = message;
    toastEl.className = variant === "success" ? "toast toast-success" : "toast";
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      toastEl.hidden = true;
    }, 4000);
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function renderEmpty(container, icon, message) {
    container.innerHTML = `<div class="empty"><span class="empty-icon">${icon}</span>${escapeHtml(message)}</div>`;
  }

  function applyTheme() {
    const scheme = tg?.colorScheme === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", scheme);
    try {
      tg?.setHeaderColor?.(scheme === "dark" ? "#1a1014" : "#fdf4f7");
      tg?.setBackgroundColor?.(scheme === "dark" ? "#1a1014" : "#fdf4f7");
    } catch (err) {
      // Clients Telegram antérieurs à Bot API 6.1.
    }
  }

  function cartItemCount() {
    let count = 0;
    for (const entry of cartByProduct.values()) {
      count += entry.quantity;
    }
    return count;
  }

  function updateCartChrome() {
    const count = cartItemCount();
    if (shopBadgeEl) {
      const badgeCount = pendingOrder ? 1 : count;
      shopBadgeEl.hidden = badgeCount === 0;
      shopBadgeEl.textContent = String(badgeCount);
    }
    if (!cartStripEl) return;
    if (pendingOrder) {
      cartStripEl.hidden = false;
      cartStripEl.innerHTML = `
        <span>Commande <strong>#${pendingOrder.order_id}</strong> en attente</span>
        <span class="cart-strip-meta">${formatPrice(pendingOrder.total_cents, pendingOrder.currency)}</span>
      `;
      return;
    }
    if (count === 0 || views.shop.hidden) {
      cartStripEl.hidden = true;
      return;
    }
    cartStripEl.hidden = false;
    cartStripEl.innerHTML = `
      <span><strong>${count}</strong> ${count > 1 ? "pièces" : "pièce"}</span>
      <span class="cart-strip-meta">${formatPrice(cartTotalCents, cartCurrency)}</span>
    `;
  }

  function stagger(elements) {
    elements.forEach((el, index) => {
      el.style.animationDelay = `${Math.min(index, 6) * 55}ms`;
    });
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
    window.scrollTo({ top: 0, behavior: "smooth" });

    if (tab === "wheel") refreshWheelStatus();
    if (tab === "vip") refreshVipStatus();
    updateCartChrome();
    updateMainButton();
  }

  for (const btn of tabButtons) {
    btn.addEventListener("click", () => {
      tg?.HapticFeedback?.selectionChanged?.();
      switchTab(btn.dataset.tab);
    });
  }

  // ------------------------------------------------------------------
  // Boutique
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
    cartCurrency = cart.items[0]?.currency || cart.pending_order?.currency || cartCurrency;
    pendingOrder = cart.pending_order || null;
  }

  function shopIsLocked() {
    if (!pendingOrder) return false;
    showToast(`La commande #${pendingOrder.order_id} attend encore le règlement.`);
    openPaymentSheet(pendingOrder);
    return true;
  }

  function renderShop() {
    renderPhotoList();
    renderCallList();
    updateCartChrome();
    updateMainButton();
  }

  function renderPhotoList() {
    if (!photos.length) {
      renderEmpty(photoListEl, "★", "Aucun booster en stock pour le moment.");
      photoNoteEl.textContent = "";
      return;
    }
    photoNoteEl.textContent = `${photos.length} boosters`;
    photoListEl.innerHTML = "";
    const featuredId = photos.reduce(
      (best, product) => (product.price_cents > best.price_cents ? product : best),
      photos[0]
    ).id;
    photos.forEach((product, index) => {
      photoListEl.appendChild(renderPhotoCard(product, index, product.id === featuredId));
    });
    stagger([...photoListEl.children]);
  }

  function renderPhotoCard(product, index, featured) {
    const entry = cartByProduct.get(product.id);
    const quantity = entry?.quantity || 0;
    const tier = BOOSTER_TIERS[Math.min(index, BOOSTER_TIERS.length - 1)];
    const rarityClass = `booster-${tier.rarity.toLowerCase()}`;

    const pack = document.createElement("article");
    pack.className = `booster ${rarityClass}${featured ? " booster-featured" : ""}${quantity ? " in-cart" : ""}`;
    pack.innerHTML = `
      <div class="booster-shine" aria-hidden="true"></div>
      <div class="booster-stars" aria-hidden="true"></div>
      <header class="booster-header">
        <span class="booster-set">NEEGY · SET 01</span>
        <span class="booster-rarity">${tier.rarity}</span>
      </header>
      <div class="booster-window">
        <span class="booster-energy" aria-hidden="true"></span>
        <p class="booster-title">${escapeHtml(product.name)}</p>
        <p class="booster-cards">${escapeHtml(tier.cards)}</p>
      </div>
      <p class="booster-banner">BOOSTER PACK</p>
      <p class="booster-desc">${escapeHtml(product.description)}</p>
      <footer class="booster-foot"></footer>
    `;

    const foot = pack.querySelector(".booster-foot");
    const price = document.createElement("span");
    price.className = "booster-price";
    price.textContent = formatPrice(product.price_cents, product.currency);

    const actionBtn = document.createElement("button");
    if (quantity > 0) {
      actionBtn.className = "btn btn-ghost booster-add";
      actionBtn.textContent = "Retirer";
      actionBtn.addEventListener("click", (event) => {
        event.stopPropagation();
        removeCartItem(product.id);
      });
    } else {
      actionBtn.className = "btn btn-gold booster-add";
      actionBtn.textContent = "Ouvrir";
      actionBtn.addEventListener("click", (event) => {
        event.stopPropagation();
        if (shopIsLocked()) return;
        changeQuantity(product.id, 1);
      });
    }
    foot.append(price, actionBtn);

    return pack;
  }

  function renderCallList() {
    if (!calls.length) {
      renderEmpty(callListEl, "○", "Aucun créneau n'est ouvert pour l'instant.");
      return;
    }
    callListEl.innerHTML = "";
    for (const product of calls) {
      callListEl.appendChild(renderCallCard(product));
    }
    stagger([...callListEl.children]);
  }

  function renderCallCard(product) {
    const entry = cartByProduct.get(product.id);
    const duration = product.duration_minutes || 15;
    const tier = CALL_TIERS[duration] || { rarity: "FIRE", energy: "feu" };
    const rarityClass = `booster-${tier.rarity.toLowerCase()}`;

    const pack = document.createElement("article");
    pack.className = `booster booster-call ${rarityClass}${entry?.call_slot_id ? " in-cart" : ""}`;

    const row = document.createElement("div");
    row.className = "booster-inner";
    row.innerHTML = `
      <div class="booster-shine" aria-hidden="true"></div>
      <div class="booster-stars" aria-hidden="true"></div>
      <header class="booster-header">
        <span class="booster-set">NEEGY · LIVE</span>
        <span class="booster-rarity">${tier.rarity}</span>
      </header>
      <div class="booster-window">
        <span class="booster-energy" aria-hidden="true"></span>
        <p class="booster-title">${escapeHtml(product.name)}</p>
        <p class="booster-cards">${duration} min</p>
      </div>
      <p class="booster-banner">ENERGY PACK</p>
      <p class="booster-desc">${escapeHtml(product.description)}</p>
      <footer class="booster-foot">
        <span class="booster-price">${formatPrice(product.price_cents, product.currency)}</span>
      </footer>
    `;
    pack.appendChild(row);

    const foot = row.querySelector(".booster-foot");

    if (entry?.call_slot_id) {
      const booked = document.createElement("div");
      booked.className = "slot-booked";
      booked.innerHTML = `
        <span class="slot-booked-label">
          <small>Créneau choisi</small>
          ${escapeHtml(formatDateTime(entry.call_slot_start_at))}
        </span>
      `;
      const removeBtn = document.createElement("button");
      removeBtn.className = "btn btn-danger";
      removeBtn.textContent = "Retirer";
      removeBtn.addEventListener("click", () => removeCartItem(product.id));
      booked.appendChild(removeBtn);
      pack.appendChild(booked);
      return pack;
    }

    const isOpen = openSlotPickers.has(product.id);
    const chooseBtn = document.createElement("button");
    chooseBtn.className = isOpen ? "btn btn-ghost booster-add" : "btn btn-gold booster-add";
    chooseBtn.textContent = isOpen ? "Masquer" : "Réserver";
    chooseBtn.addEventListener("click", () => {
      if (!isOpen && shopIsLocked()) return;
      toggleSlotPicker(product);
    });
    foot.appendChild(chooseBtn);

    if (isOpen) {
      const picker = document.createElement("div");
      picker.className = "slot-picker";
      const cached = slotCache.get(product.id);
      if (!cached) {
        picker.innerHTML = '<div class="skeleton" style="height: 38px; width: 100%"></div>';
      } else if (!cached.length) {
        picker.innerHTML =
          '<p class="section-note">Aucun créneau ouvert pour cette durée pour l\'instant.</p>';
      } else {
        for (const slot of cached) {
          const chip = document.createElement("button");
          chip.className = "slot-chip";
          chip.textContent = formatDateTime(slot.start_at);
          chip.addEventListener("click", () => bookSlot(product, slot));
          picker.appendChild(chip);
        }
      }
      pack.appendChild(picker);
    }

    return pack;
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
        showToast(err.message);
      }
      renderCallList();
    }
  }

  async function bookSlot(product, slot) {
    if (shopIsLocked()) return;
    try {
      const cart = await apiFetch("/api/cart", {
        method: "POST",
        body: JSON.stringify({ product_id: product.id, quantity: 1, call_slot_id: slot.id }),
      });
      applyCart(cart);
      openSlotPickers.delete(product.id);
      slotCache.delete(product.id);
      renderShop();
      tg?.HapticFeedback?.notificationOccurred?.("success");
      showToast(`Créneau choisi : ${formatDateTime(slot.start_at)}`, "success");
    } catch (err) {
      showToast(err.message);
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
      tg?.HapticFeedback?.impactOccurred?.("light");
    } catch (err) {
      showToast(err.message);
    }
  }

  async function removeCartItem(productId) {
    try {
      const cart = await apiFetch(`/api/cart/${productId}`, { method: "DELETE" });
      applyCart(cart);
      renderShop();
      tg?.HapticFeedback?.impactOccurred?.("light");
    } catch (err) {
      showToast(err.message);
    }
  }

  function updateMainButton() {
    if (!tg) return;
    if (views.shop.hidden) {
      tg.MainButton.hide();
      return;
    }
    if (pendingOrder) {
      tg.MainButton.setText(
        `Régler · ${formatPrice(pendingOrder.total_cents, pendingOrder.currency)}`
      );
      tg.MainButton.show();
      return;
    }
    if (cartTotalCents <= 0) {
      tg.MainButton.hide();
      return;
    }
    tg.MainButton.setText(`Commander · ${formatPrice(cartTotalCents, cartCurrency)}`);
    tg.MainButton.show();
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      const field = document.createElement("textarea");
      field.value = text;
      field.setAttribute("readonly", "");
      field.style.position = "fixed";
      field.style.opacity = "0";
      document.body.appendChild(field);
      field.select();
      const ok = document.execCommand("copy");
      field.remove();
      return ok;
    }
  }

  function setPointsBalance(balance) {
    if (!pointsBalanceEl) return;
    const n = Number(balance);
    pointsBalanceEl.textContent = Number.isFinite(n) ? String(n) : "·";
  }

  function openPaymentSheet(result) {
    const payment = result.payment || {};
    lastPayOrder = result;
    payHeadingEl.textContent = `Commande #${result.order_id}`;
    payTotalEl.textContent = formatPrice(result.total_cents, result.currency);

    const needed = Number(result.points_needed) || 0;
    const balance = Number(result.points_balance) || 0;
    if (payPointsHintEl) {
      if (needed <= 0) {
        payPointsHintEl.hidden = true;
      } else if (balance >= needed) {
        payPointsHintEl.hidden = false;
        payPointsHintEl.textContent = `Tu as ${balance} points. Ce panier coûte ${needed} pts (1 pt = 1 €).`;
      } else {
        payPointsHintEl.hidden = false;
        payPointsHintEl.textContent = `${balance} / ${needed} points — continue avec PayPal ou un virement.`;
      }
    }
    if (payPointsEl) {
      payPointsEl.hidden = !(needed > 0 && balance >= needed);
      payPointsEl.textContent = `Payer avec ${needed} points`;
    }

    if (payCryptoOpenEl) {
      const hasCrypto = Boolean(
        payment.crypto_solana || payment.crypto_ethereum || payment.crypto_bitcoin
      );
      payCryptoOpenEl.hidden = !hasCrypto;
    }

    if (payment.paypal_url) {
      payPaypalEl.hidden = false;
      payPaypalEl.href = payment.paypal_url;
    } else {
      payPaypalEl.hidden = true;
      payPaypalEl.removeAttribute("href");
    }

    if (payment.bank_iban || payment.bank_holder) {
      payBankEl.hidden = false;
      payHolderEl.textContent = payment.bank_holder || "";
      payHolderEl.hidden = !payment.bank_holder;
      payIbanCopyEl.hidden = !payment.bank_iban;
      payIbanCopyEl.textContent = payment.bank_iban || "";
      payIbanCopyEl.dataset.iban = (payment.bank_iban || "").replace(/\s+/g, "");
      payRefEl.textContent = payment.reference
        ? `Libellé : ${payment.reference}`
        : "";
    } else {
      payBankEl.hidden = true;
    }

    paySheetEl.hidden = false;
    tg?.MainButton?.hide();
  }

  async function handleCheckout() {
    if (pendingOrder) {
      openPaymentSheet(pendingOrder);
      return;
    }
    tg?.MainButton?.showProgress?.(true);
    try {
      const result = await apiFetch("/api/checkout", { method: "POST" });
      cartByProduct = new Map();
      cartTotalCents = 0;
      pendingOrder = result;
      renderShop();
      tg?.HapticFeedback?.notificationOccurred?.("success");
      openPaymentSheet(result);
    } catch (err) {
      tg?.HapticFeedback?.notificationOccurred?.("error");
      showToast(err.message);
    } finally {
      tg?.MainButton?.hideProgress?.();
    }
  }

  // ------------------------------------------------------------------
  // Roues
  // ------------------------------------------------------------------

  function renderWheelSwitch() {
    if (!wheelSwitchEl) return;
    wheelSwitchEl.innerHTML = "";
    const fallback = [
      { slug: "free", name: "Quotidienne", price_cents: 0 },
      { slug: "rose", name: "Rose", price_cents: 500 },
      { slug: "nuit", name: "Nuit", price_cents: 2000 },
    ];
    const list = wheelsCatalog.length ? wheelsCatalog : fallback;
    list.forEach((wheel) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `wheel-switch-btn${wheel.slug === selectedWheelSlug ? " active" : ""}`;
      const price =
        wheel.price_cents > 0 ? formatPrice(wheel.price_cents, "EUR") : "gratuite";
      btn.innerHTML = `<strong>${escapeHtml(wheel.name)}</strong>${escapeHtml(price)}`;
      btn.addEventListener("click", () => {
        selectedWheelSlug = wheel.slug;
        wheelRotation = 0;
        if (wheelGraphicEl) wheelGraphicEl.style.transform = "rotate(0deg)";
        renderWheelSwitch();
        paintWheel();
        syncWheelCta();
        tg?.HapticFeedback?.selectionChanged?.();
      });
      wheelSwitchEl.appendChild(btn);
    });
  }

  function syncWheelCta() {
    const wheel = currentWheel();
    if (!wheelSpinBtn || !wheelStatusEl) return;
    if (!wheel || wheel.slug === "free") {
      return;
    }
    wheelSpinBtn.disabled = false;
    wheelSpinBtn.textContent = `Tenter · ${formatPrice(wheel.price_cents, "EUR")}`;
    wheelStatusEl.textContent = "Tu paies. Ensuite, on voit.";
  }

  async function refreshWheelStatus() {
    wheelSpinBtn.disabled = true;
    try {
      const payload = await apiFetch("/api/wheels");
      wheelsCatalog = payload.wheels || [];
      setPointsBalance(payload.points_balance);
      if (!wheelsCatalog.some((wheel) => wheel.slug === selectedWheelSlug)) {
        selectedWheelSlug = "free";
      }
      renderWheelSwitch();
      paintWheel();
      const selected = currentWheel();
      if (!selected || selected.slug === "free") {
        if (selected?.can_spin) {
          wheelStatusEl.textContent = "Personne ne sait ce qui va tomber.";
          wheelSpinBtn.disabled = false;
          wheelSpinBtn.textContent = "Tourner la roue";
        } else {
          wheelStatusEl.textContent = "C'est joué pour aujourd'hui. Reviens demain.";
          wheelSpinBtn.textContent = "Reviens demain";
        }
      } else {
        syncWheelCta();
      }
    } catch (err) {
      wheelStatusEl.textContent = "Impossible de charger la roue pour le moment.";
      showToast(err.message);
    }
  }

  async function buyPaidSpin(wheel) {
    if (!wheel.product_id) {
      showToast("Cette roue n'est pas encore ouverte.");
      return;
    }
    if (shopIsLocked()) return;
    wheelSpinBtn.disabled = true;
    try {
      const cart = await apiFetch("/api/cart", {
        method: "POST",
        body: JSON.stringify({ product_id: wheel.product_id, quantity: 1 }),
      });
      applyCart(cart);
      renderShop();
      await handleCheckout();
    } catch (err) {
      showToast(err.message);
    } finally {
      wheelSpinBtn.disabled = false;
      syncWheelCta();
    }
  }

  function animateSpinTo(points) {
    animateSpinToPrize({ kind: "points", points_amount: points, label: String(points) });
  }

  wheelSpinBtn.addEventListener("click", async () => {
    const wheel = currentWheel();
    if (wheel && wheel.slug !== "free") {
      await buyPaidSpin(wheel);
      return;
    }
    wheelSpinBtn.disabled = true;
    wheelStatusEl.textContent = "Encore un instant.";
    tg?.HapticFeedback?.impactOccurred?.("medium");

    let prize;
    try {
      prize = await apiFetch("/api/wheel/spin", { method: "POST" });
    } catch (err) {
      showToast(err.message);
      await refreshWheelStatus();
      return;
    }

    animateSpinToPrize(prize);
    await new Promise((resolve) => setTimeout(resolve, SPIN_DURATION_MS + 280));

    tg?.HapticFeedback?.notificationOccurred?.("success");
    if (typeof prize.points_balance === "number") setPointsBalance(prize.points_balance);
    if (tg?.showPopup) {
      tg.showPopup({
        title: prize.label || String(prize.points_amount || ""),
        message: prize.description || "C'est tombé.",
        buttons: [{ type: "close" }],
      });
    } else {
      showToast(prize.description || prize.label, "success");
    }
    await refreshWheelStatus();
  });

  // ------------------------------------------------------------------
  // VIP
  // ------------------------------------------------------------------

  async function refreshVipStatus() {
    try {
      const [status, vipProducts] = await Promise.all([
        apiFetch("/api/vip/status"),
        apiFetch("/api/products?category=vip"),
      ]);
      renderVip(status, vipProducts);
    } catch (err) {
      renderEmpty(vipContentEl, "♔", "Le cercle est inaccessible pour le moment.");
      showToast(err.message);
    }
  }

  function renderVip(status, vipProducts) {
    vipContentEl.innerHTML = "";

    if (status.active) {
      const hero = document.createElement("div");
      hero.className = "vip-hero";
      hero.innerHTML = `
        <p class="vip-crown">♔</p>
        <p class="badge">Membre du cercle</p>
        <p class="vip-plan-name">${escapeHtml(status.plan_name || "VIP")}</p>
        <p class="vip-expiry">Ton accès court jusqu'au ${escapeHtml(formatDate(status.expires_at))}</p>
      `;
      vipContentEl.appendChild(hero);
      return;
    }

    if (!vipProducts.length) {
      renderEmpty(vipContentEl, "♔", "Aucune formule n'est ouverte pour le moment.");
      return;
    }

    for (const product of vipProducts) {
      const entry = cartByProduct.get(product.id);
      const card = document.createElement("div");
      card.className = "vip-hero";

      // La description en base fait foi : si elle contient des puces (retours
      // à la ligne, · ou •), on l'affiche en liste d'avantages.
      const parts = String(product.description)
        .split(/\r?\n|·|•/)
        .map((part) => part.trim())
        .filter(Boolean);
      const details =
        parts.length > 1
          ? `<ul class="perks">${parts.map((p) => `<li>${escapeHtml(p)}</li>`).join("")}</ul>`
          : `<p class="vip-expiry">${escapeHtml(product.description)}</p>`;

      card.innerHTML = `
        <p class="vip-crown">♔</p>
        <p class="vip-plan-name">${escapeHtml(product.name)}</p>
        <p class="vip-price">${formatPrice(product.price_cents, product.currency)}<small> / mois</small></p>
        ${details}
      `;

      const button = document.createElement("button");
      if (entry?.quantity) {
        button.className = "btn btn-ghost btn-block";
        button.textContent = "Retirer du panier";
        button.addEventListener("click", async () => {
          await removeCartItem(product.id);
          await refreshVipStatus();
        });
      } else {
        button.className = "btn btn-gold btn-block";
        button.textContent = "Demander l'accès";
        button.addEventListener("click", async () => {
          if (shopIsLocked()) return;
          await changeQuantity(product.id, 1);
          await refreshVipStatus();
          showToast("Ajouté au panier — valide depuis l'onglet Boutique.", "success");
        });
      }
      card.appendChild(button);
      vipContentEl.appendChild(card);
    }

    const hint = document.createElement("p");
    hint.className = "vip-hint";
    hint.textContent = "La commande se conclut depuis l'onglet Boutique.";
    vipContentEl.appendChild(hint);
  }

  // ------------------------------------------------------------------
  // Démarrage
  // ------------------------------------------------------------------

  async function init() {
    if (tg) {
      tg.ready();
      tg.expand();
      applyTheme();
      tg.onEvent?.("themeChanged", applyTheme);
      tg.MainButton.onClick(handleCheckout);
      try {
        tg.MainButton.setParams({ color: "#e8a0b8", text_color: "#4a2432" });
      } catch (err) {
        // setParams n'existe pas sur les clients Telegram antérieurs à Bot API 6.1.
      }
    } else {
      applyTheme();
    }

    payPaypalEl.addEventListener("click", (event) => {
      const url = payPaypalEl.getAttribute("href");
      if (!url) {
        event.preventDefault();
        return;
      }
      if (tg?.openLink) {
        event.preventDefault();
        tg.openLink(url);
      }
    });

    payIbanCopyEl.addEventListener("click", async () => {
      const iban = payIbanCopyEl.dataset.iban || payIbanCopyEl.textContent;
      if (!iban) return;
      const copied = await copyText(iban);
      showToast(copied ? "IBAN copié" : "Impossible de copier", copied ? "success" : undefined);
      tg?.HapticFeedback?.impactOccurred?.("light");
    });

    function fillCryptoSheet(payment) {
      if (!cryptoListEl) return;
      cryptoListEl.innerHTML = "";
      const rows = [
        ["Solana", payment.crypto_solana],
        ["Ethereum", payment.crypto_ethereum],
        ["Bitcoin", payment.crypto_bitcoin],
      ];
      for (const [label, address] of rows) {
        if (!address) continue;
        const wrap = document.createElement("div");
        wrap.className = "crypto-row";
        const title = document.createElement("p");
        title.className = "crypto-row-label";
        title.textContent = label;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "pay-iban";
        btn.textContent = address;
        btn.addEventListener("click", async () => {
          const copied = await copyText(address);
          showToast(copied ? `${label} copié` : "Impossible de copier", copied ? "success" : undefined);
          tg?.HapticFeedback?.impactOccurred?.("light");
        });
        wrap.appendChild(title);
        wrap.appendChild(btn);
        cryptoListEl.appendChild(wrap);
      }
    }

    payCryptoOpenEl?.addEventListener("click", () => {
      fillCryptoSheet((lastPayOrder && lastPayOrder.payment) || {});
      if (cryptoSheetEl) cryptoSheetEl.hidden = false;
    });

    cryptoBackEl?.addEventListener("click", () => {
      if (cryptoSheetEl) cryptoSheetEl.hidden = true;
    });

    payPointsEl?.addEventListener("click", async () => {
      if (!lastPayOrder?.order_id) return;
      payPointsEl.disabled = true;
      try {
        const paid = await apiFetch(`/api/orders/${lastPayOrder.order_id}/pay-points`, {
          method: "POST",
        });
        setPointsBalance(paid.points_balance);
        payPaypalEl.hidden = true;
        payBankEl.hidden = true;
        payPointsEl.hidden = true;
        if (payCryptoOpenEl) payCryptoOpenEl.hidden = true;
        if (payPointsHintEl) {
          payPointsHintEl.hidden = false;
          payPointsHintEl.textContent = `Payé avec ${paid.points_spent} points. Solde : ${paid.points_balance} pts.`;
        }
        payTotalEl.textContent = "Réglé en points";
        pendingOrder = null;
        showToast("Commande payée avec tes points", "success");
        tg?.HapticFeedback?.notificationOccurred?.("success");
        try {
          await fetchCatalog();
          renderShop();
        } catch (err) {
          renderShop();
        }
      } catch (err) {
        showToast(err.message);
      } finally {
        payPointsEl.disabled = false;
      }
    });

    payDoneEl.addEventListener("click", () => {
      paySheetEl.hidden = true;
      if (cryptoSheetEl) cryptoSheetEl.hidden = true;
    });

    paintWheel();
    switchTab("shop");

    try {
      await fetchCatalog();
      renderShop();
    } catch (err) {
      showToast(err.message);
      renderEmpty(photoListEl, "◇", "Impossible de charger la sélection.");
      callListEl.innerHTML = "";
    }
  }

  init();
})();
