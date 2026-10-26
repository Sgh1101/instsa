// 설치 가능한 앱(PWA)과 공유 대상 등록을 위한 최소 서비스 워커입니다. 캐시는 하지 않고 항상 네트워크를 씁니다.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
