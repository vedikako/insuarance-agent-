const STEPS = [
  {
    title: "This is the home screen",
    body: "Use it as the map. Each card opens one job. You can skip this guide and come back later with the Guide button.",
  },
  {
    title: "Sign in on the page you open",
    body: "Ask, file, status, and review each start with email and password. A customer only sees their own policies and claims. The insurance company reviews claims. An auditor can look, but cannot accept or reject.",
  },
  {
    title: "Ask about a policy",
    body: "Open Ask. Choose one policy, then ask what it covers or how much is left. This chat does not approve a claim.",
    href: "/chat",
    label: "Go to Ask",
  },
  {
    title: "File a claim",
    body: "Open File a claim. Pick the policy, enter the amount and what happened, then upload the bill and an identity document.",
    href: "/file",
    label: "Go to File a claim",
  },
  {
    title: "Check the status",
    body: "After you file, open Status to see whether the claim is still waiting or has been accepted or rejected.",
    href: "/status",
    label: "Go to Status",
  },
  {
    title: "The company still decides",
    body: "An insurance company user opens Review, runs the recommendation, and then accepts or rejects. The agents do not make that final choice.",
    href: "/review",
    label: "Go to Review",
  },
];

const STORAGE_KEY = "insureflow_guide_v1";

function currentPath() {
  const path = window.location.pathname;
  return path.endsWith("/") && path.length > 1 ? path.slice(0, -1) : path;
}

function paintNav() {
  const slot = document.getElementById("site-nav");
  if (!slot) return;
  const here = currentPath() || "/";
  const links = [
    ["/", "Home"],
    ["/chat", "Ask"],
    ["/file", "File a claim"],
    ["/status", "Status"],
    ["/review", "Review"],
  ];
  slot.className = "site-nav";
  slot.innerHTML = links
    .map(([href, label]) => {
      const current = href === here ? ' aria-current="page"' : "";
      return `<a href="${href}"${current}>${label}</a>`;
    })
    .join("");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "guide-open";
  button.textContent = "Guide";
  button.addEventListener("click", () => openGuide(0));
  slot.appendChild(button);
}

function openGuide(start) {
  const backdrop = document.getElementById("guide");
  if (!backdrop) return;
  backdrop.hidden = false;
  backdrop.dataset.step = String(start || 0);
  renderStep();
}

function closeGuide(remember) {
  const backdrop = document.getElementById("guide");
  if (!backdrop) return;
  backdrop.hidden = true;
  if (remember) localStorage.setItem(STORAGE_KEY, "done");
}

function renderStep() {
  const backdrop = document.getElementById("guide");
  const index = Number(backdrop.dataset.step || 0);
  const step = STEPS[index];
  document.getElementById("guide-progress").textContent = "Step " + (index + 1) + " of " + STEPS.length;
  document.getElementById("guide-title").textContent = step.title;
  const body = document.getElementById("guide-body");
  body.textContent = step.body;
  const link = document.getElementById("guide-link");
  if (step.href) {
    link.hidden = false;
    link.href = step.href;
    link.textContent = step.label;
  } else {
    link.hidden = true;
  }
  document.getElementById("guide-back").hidden = index === 0;
  document.getElementById("guide-next").textContent = index === STEPS.length - 1 ? "Done" : "Next";
}

function installGuide() {
  if (document.getElementById("guide")) return;
  const backdrop = document.createElement("div");
  backdrop.id = "guide";
  backdrop.className = "guide-backdrop";
  backdrop.hidden = true;
  backdrop.innerHTML = `
    <div class="guide-card" role="dialog" aria-modal="true" aria-labelledby="guide-title">
      <div id="guide-progress" class="guide-progress"></div>
      <h2 id="guide-title"></h2>
      <p id="guide-body"></p>
      <p><a id="guide-link" href="/"></a></p>
      <div class="guide-actions">
        <button id="guide-back" class="quiet" type="button">Back</button>
        <button id="guide-next" type="button">Next</button>
        <button id="guide-skip" class="quiet" type="button">Skip guide</button>
      </div>
    </div>`;
  document.body.appendChild(backdrop);
  document.getElementById("guide-back").addEventListener("click", () => {
    backdrop.dataset.step = String(Math.max(0, Number(backdrop.dataset.step) - 1));
    renderStep();
  });
  document.getElementById("guide-next").addEventListener("click", () => {
    const index = Number(backdrop.dataset.step || 0);
    if (index >= STEPS.length - 1) {
      closeGuide(true);
      return;
    }
    backdrop.dataset.step = String(index + 1);
    renderStep();
  });
  document.getElementById("guide-skip").addEventListener("click", () => closeGuide(true));
}

paintNav();
installGuide();

const params = new URLSearchParams(window.location.search);
const here = currentPath() || "/";
if (params.get("guide") === "1" || (here === "/" && !localStorage.getItem(STORAGE_KEY))) {
  openGuide(0);
}
