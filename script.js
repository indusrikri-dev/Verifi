const currencyFormatter = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 0,
});

const statementUpload = document.querySelector("#statementUpload");
const sampleReviewButton = document.querySelector("#sampleReviewButton");
const reviewStatus = document.querySelector("#reviewStatus");
const reviewSource = document.querySelector("#reviewSource");
const alertCount = document.querySelector("#alertCount");
const familyStatus = document.querySelector("#familyStatus");
const familyForm = document.querySelector("#familyForm");
const familyMessage = document.querySelector("#familyMessage");
const termsInput = document.querySelector("#termsInput");
const termsResult = document.querySelector("#termsResult");
const checkTermsButton = document.querySelector("#checkTermsButton");
const reviewSpinner = document.querySelector("#reviewSpinner");
const termsSpinner = document.querySelector("#termsSpinner");
const voiceButton = document.querySelector("#voiceButton");
const readSummaryButton = document.querySelector("#readSummaryButton");
const safetyChecklistButton = document.querySelector("#safetyChecklistButton");
const safetyDialog = document.querySelector("#safetyDialog");
const closeSafetyDialog = document.querySelector("#closeSafetyDialog");

let currentExplanation = "There is no statement summary to read yet.";
let currentReview = null;
let voiceEnabled = false;

function setText(selector, value) {
  document.querySelector(selector).textContent = value;
}

function speak(message) {
  if (!("speechSynthesis" in window)) {
    return;
  }

  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(message);
  utterance.rate = 0.86;
  utterance.pitch = 0.95;
  window.speechSynthesis.speak(utterance);
}

async function readErrorMessage(response) {
  try {
    const data = await response.json();
    return data.error || `Something went wrong (status ${response.status}).`;
  } catch {
    return `Something went wrong (status ${response.status}).`;
  }
}

function renderReview(review) {
  reviewSpinner.hidden = true;
  currentReview = review;
  currentExplanation = review.explanation;
  reviewStatus.textContent = "Review complete";
  reviewSource.textContent = review.source;
  alertCount.textContent = `${review.alerts.length} alert${review.alerts.length === 1 ? "" : "s"}`;

  setText("#moneyIn", currencyFormatter.format(review.moneyIn));
  setText("#moneyOut", currencyFormatter.format(review.moneyOut));
  setText("#leftOver", currencyFormatter.format(review.leftOver));
  setText("#incomeNote", "Income, benefits, refunds, and deposits.");
  setText("#spendingNote", "Bills, shopping, cash withdrawals, and transfers.");
  setText("#leftOverNote", review.leftOver >= 0 ? "You spent less than came in." : "You spent more than came in.");
  setText("#plainExplanation", review.explanation);

  const alertsList = document.querySelector("#alertsList");
  alertsList.innerHTML = "";
  if (review.alerts.length === 0) {
    const item = document.createElement("li");
    item.textContent = "Nothing unusual was found in this statement.";
    alertsList.append(item);
  } else {
    review.alerts.forEach((alert) => {
      const item = document.createElement("li");
      item.textContent = alert;
      alertsList.append(item);
    });
  }

  if (voiceEnabled) {
    speak(`Your review is complete. ${review.explanation}`);
  }
}

function setReviewLoading(message) {
  reviewSpinner.hidden = false;
  reviewStatus.textContent = message;
  reviewSource.textContent = "Please wait...";
}

function setReviewError(message) {
  reviewSpinner.hidden = true;
  reviewStatus.textContent = "Review failed";
  reviewSource.textContent = message;
  setText("#plainExplanation", message);
}

sampleReviewButton.addEventListener("click", async () => {
  sampleReviewButton.disabled = true;
  setReviewLoading("Reviewing sample statement...");

  try {
    const response = await fetch("/api/review/sample", { method: "POST" });
    if (!response.ok) {
      throw new Error(await readErrorMessage(response));
    }
    renderReview(await response.json());
  } catch (error) {
    setReviewError(error.message);
  } finally {
    sampleReviewButton.disabled = false;
  }
});

statementUpload.addEventListener("change", async (event) => {
  const [file] = event.target.files;
  if (!file) {
    return;
  }

  setReviewLoading("Reviewing your statement...");

  try {
    const formData = new FormData();
    formData.append("statement", file);
    const response = await fetch("/api/review", { method: "POST", body: formData });
    if (!response.ok) {
      throw new Error(await readErrorMessage(response));
    }
    renderReview(await response.json());
  } catch (error) {
    setReviewError(error.message);
  } finally {
    statementUpload.value = "";
  }
});

familyForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(familyForm);
  const name = String(data.get("familyName") || "").trim();
  const email = String(data.get("familyEmail") || "").trim();

  if (!name || !email) {
    familyMessage.textContent = "Please add a name and email before sending.";
    return;
  }

  if (!currentReview) {
    familyMessage.textContent = "Review a statement first, then share it with your helper.";
    return;
  }

  const submitButton = familyForm.querySelector("button[type=submit]");
  submitButton.disabled = true;
  familyMessage.textContent = "Creating a read-only share link...";

  try {
    const response = await fetch("/api/share", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ review: currentReview, name, email }),
    });
    if (!response.ok) {
      throw new Error(await readErrorMessage(response));
    }
    const { url } = await response.json();

    familyStatus.textContent = `Sharing with ${name}`;
    familyMessage.innerHTML = "";
    const label = document.createElement("span");
    label.textContent = `Send this read-only link to ${name}: `;
    const link = document.createElement("a");
    link.href = url;
    link.textContent = url;
    link.target = "_blank";
    link.rel = "noopener";
    familyMessage.append(label, link);

    if (voiceEnabled) {
      speak(`A read-only link has been created to share with ${name}.`);
    }
  } catch (error) {
    familyMessage.textContent = error.message;
  } finally {
    submitButton.disabled = false;
  }
});

checkTermsButton.addEventListener("click", async () => {
  const text = termsInput.value.trim();

  if (!text) {
    termsResult.textContent = "Paste the terms first, then press Check terms.";
    return;
  }

  checkTermsButton.disabled = true;
  termsSpinner.hidden = false;
  termsResult.textContent = "Checking terms...";

  try {
    const response = await fetch("/api/terms-check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!response.ok) {
      throw new Error(await readErrorMessage(response));
    }
    const result = await response.json();

    const paragraphs = [result.summary, ...result.warnings];
    termsResult.innerHTML = paragraphs.map((p) => `<p>${p}</p>`).join("");

    if (voiceEnabled) {
      speak(paragraphs.join(" "));
    }
  } catch (error) {
    termsResult.textContent = error.message;
  } finally {
    checkTermsButton.disabled = false;
    termsSpinner.hidden = true;
  }
});

voiceButton.addEventListener("click", () => {
  voiceEnabled = !voiceEnabled;
  voiceButton.setAttribute("aria-pressed", String(voiceEnabled));
  voiceButton.textContent = voiceEnabled ? "Voice help on" : "Voice help";

  if (voiceEnabled) {
    speak("Voice help is on. I can read summaries and warnings aloud.");
  } else if ("speechSynthesis" in window) {
    window.speechSynthesis.cancel();
  }
});

readSummaryButton.addEventListener("click", () => {
  speak(currentExplanation);
});

safetyChecklistButton.addEventListener("click", () => {
  safetyDialog.showModal();
});

closeSafetyDialog.addEventListener("click", () => {
  safetyDialog.close();
});
