import { AiConfig } from '@n8n/config';
import { Container } from '@n8n/di';
import type { AxiosRequestConfig } from 'axios';
import axios, { AxiosHeaders } from 'axios';
import { stringify } from 'qs';

import { setAxiosAgents } from './axios-utils';

// Global axios defaults

axios.defaults.timeout = 300000;
// Prevent axios from adding x-form-www-urlencoded headers by default
axios.defaults.headers.post = {};
axios.defaults.headers.put = {};
axios.defaults.headers.patch = {};
axios.defaults.paramsSerializer = (params) => {
	if (params instanceof URLSearchParams) {
		return params.toString();
	}
	return stringify(params, { arrayFormat: 'indices' });
};
// Disable axios proxy, we handle it ourselves
// Axios proxy option has problems: https://github.com/axios/axios/issues/4531
axios.defaults.proxy = false;

// Interceptor (side effect)
axios.interceptors.request.use((config) => {
	setAxiosAgents(config);
	applyVendorHeaders(config);

	// If no content-type is set by us, prevent axios from force-setting the content-type to
	// `application/x-www-form-urlencoded`. Run this AFTER applyVendorHeaders so that
	// config.headers is guaranteed to be an AxiosHeaders instance (not a plain object).
	if (config.data === undefined) {
		config.headers.setContentType(false, false);
	}

	return config;
});

function applyVendorHeaders(config: AxiosRequestConfig) {
	if ([config.url, config.baseURL].some((url) => url?.startsWith('https://api.openai.com/'))) {
		// Merge vendor headers while preserving the AxiosHeaders instance so that
		// methods like setContentType() remain available to the interceptor chain.
		config.headers = new AxiosHeaders({
			...Container.get(AiConfig).openAiDefaultHeaders,
			...(config.headers ?? {}),
		});
	}
}
