(function () {
  document
    .querySelectorAll("[data-product-stock-filter], [data-director-stock-filter]")
    .forEach(function (form) {
      const select = form.querySelector("select");
      if (!select) return;
      select.addEventListener("change", function () {
        form.requestSubmit();
      });
    });
})();
