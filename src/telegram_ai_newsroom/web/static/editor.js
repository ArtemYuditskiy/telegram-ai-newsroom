"use strict";

const counter = document.querySelector("[data-counter]");
if (counter) {
  const field = document.getElementById(counter.dataset.counter);
  const updateCount = () => {
    const count = Array.from(field.value.trim()).length;
    counter.textContent = counter.dataset.countFormat.replace("{count}", count);
    field.setCustomValidity(count > 700 ? counter.dataset.countError : "");
  };
  field.addEventListener("input", updateCount);
  updateCount();
}

const languagePicker = document.querySelector(".language-picker");
if (languagePicker) {
  languagePicker.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && languagePicker.open) {
      languagePicker.open = false;
      languagePicker.querySelector("summary").focus();
    }
  });
  document.addEventListener("click", (event) => {
    if (!languagePicker.contains(event.target)) languagePicker.open = false;
  });
}
