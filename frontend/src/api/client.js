import axios from 'axios';

const client = axios.create({ baseURL: '/api' });

// 크롤러 차단(423)은 서버가 준 안내 문구를 기존 화면의 detail 자리에 보인다
client.interceptors.response.use(
  (res) => res,
  (error) => {
    const data = error.response?.data;
    if (error.response?.status === 423 && data && data.message && !data.detail) {
      data.detail = data.message;
    }
    return Promise.reject(error);
  },
);

export default client;
