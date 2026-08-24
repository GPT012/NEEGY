(() => {
  "use strict";

  const tg = window.Telegram?.WebApp;
  const productListEl = document.getElementById("product-list");
  const errorBannerEl = document.getElementById("error-banner");

  /** @type {Array<{id:number,name:string,description:string,price_cents:number,currency:string}>} */
  let products = [];
  /** @type {Map<number, number>} product_id -> quantity */
  let cartQuantities = new Map();
  let cartTotalCents = 0;
  let cartCurrency = "EUR";

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

  function showError(message) {
    errorBannerEl.textContent = message;
    errorBannerEl.hidden = false;
    setTimeout(() => {
      errorBannerEl.hidden = true;
    }, 4000);
  }

  async function fetchProducts() {
    products = await apiFetch("/api/products");
  }

  async function fetchCart() {
    const cart = await apiFetch("/api/cart");
    cartQuantities = new Map(cart.items.map((item) => [item.product_id, item.quantity]));
    cartTotalCents = cart.total_cents;
    cartCurrency = cart.items[0]?.currency || "EUR";
  }

  function render() {
    if (!products.length) {
      productListEl.innerHTML = '<p class="loading">Aucun service disponible pour le moment.</p>';
      updateMainButton();
      return;
    }

    productListEl.innerHTML = "";
    for (const product of products) {
      productListEl.appendChild(renderProductCard(product));
    }
    updateMainButton();
  }

  function renderProductCard(product) {
    const quantity = cartQuantities.get(product.id) || 0;

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
      minusBtn.addEventListener("click", () => changeQuantity(product, quantity - 1));

      const value = document.createElement("span");
      value.className = "qty-value";
      value.textContent = String(quantity);

      const plusBtn = document.createElement("button");
      plusBtn.className = "qty-btn";
      plusBtn.textContent = "+";
      plusBtn.addEventListener("click", () => changeQuantity(product, quantity + 1));

      control.append(minusBtn, value, plusBtn);
      card.appendChild(control);
    } else {
      const addBtn = document.createElement("button");
      addBtn.className = "add-btn";
      addBtn.textContent = "Ajouter";
      addBtn.addEventListener("click", () => changeQuantity(product, 1));
      card.appendChild(addBtn);
    }

    return card;
  }

  function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  async function changeQuantity(product, newQuantity) {
    try {
      const cart = await apiFetch("/api/cart", {
        method: "POST",
        body: JSON.stringify({ product_id: product.id, quantity: newQuantity }),
      });
      cartQuantities = new Map(cart.items.map((item) => [item.product_id, item.quantity]));
      cartTotalCents = cart.total_cents;
      cartCurrency = cart.items[0]?.currency || cartCurrency;
      render();
      tg?.HapticFeedback?.impactOccurred("light");
    } catch (err) {
      showError(err.message);
    }
  }

  function updateMainButton() {
    if (!tg) return;

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
      cartQuantities = new Map();
      cartTotalCents = 0;
      render();
      tg.HapticFeedback?.notificationOccurred("success");
      tg.showPopup(
        {
          title: "Commande confirmée",
          message: `Commande #${result.order_id} enregistrée pour ${formatPrice(
            result.total_cents,
            result.currency
          )}. Le récapitulatif t'a été envoyé dans le chat.`,
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

  async function init() {
    if (tg) {
      tg.ready();
      tg.expand();
      tg.MainButton.onClick(handleCheckout);
    }

    try {
      await Promise.all([fetchProducts(), fetchCart()]);
      render();
    } catch (err) {
      showError(err.message);
      productListEl.innerHTML = '<p class="loading">Impossible de charger le catalogue.</p>';
    }
  }

  init();
})();
