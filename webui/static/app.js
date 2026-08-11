/* hermes-loop-r2 admin shell — minimal vanilla JS */

(function () {
  'use strict';

  // Highlight current page in top nav + sidebar
  var path = location.pathname.split('/').pop() || 'dashboard.html';
  var links = document.querySelectorAll('.nav-links a, .sidebar-link');
  for (var i = 0; i < links.length; i++) {
    var href = links[i].getAttribute('href');
    if (href && href.endsWith(path)) {
      links[i].classList.add('active');
    }
  }

  // Mobile sidebar toggle
  var toggle = document.getElementById('menu-toggle');
  var sidebar = document.getElementById('sidebar');
  if (toggle && sidebar) {
    toggle.addEventListener('click', function () {
      sidebar.classList.toggle('open');
    });

    // Close sidebar when a link is clicked on mobile
    sidebar.addEventListener('click', function (e) {
      if (e.target.tagName === 'A' && sidebar.classList.contains('open')) {
        sidebar.classList.remove('open');
      }
    });
  }
})();
